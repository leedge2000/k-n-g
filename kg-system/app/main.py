from __future__ import annotations

from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import OAuth2PasswordRequestForm

from .auth import (
    Principal,
    authenticate,
    current_principal,
    issue_token,
    principal_from_token,
    require_roles,
)
from .archive import ResponseArchive, guess_media_type
from .config import Settings
from .crawler import Crawler
from .db import Database
from .documents import DocumentIngestor
from .excel import ExcelKnowledgeImporter, build_template_workbook
from .governance import GovernanceService
from .market import MarketError, MarketService
from .nebula import NebulaGraphStore
from .ontology import OntologyService
from .ops import OpsScheduler, OpsService
from .policy import PolicyService
from .conclusion import ConclusionService
from .pack2b import Pack2bService, ProtectedAssetError
from .pack2c import Pack2cService
from .rag import RagService
from .reports import ReportService
from .scheduler import CrawlScheduler
from .scope import ScopeError, ScopeService
from .service import KnowledgeService
from .storage import ElasticsearchSegmentIndex, LocalSnapshotStore, MinioSnapshotStore


def offline_document_html(row: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    """用数据库中已保存的标题、摘要和正文生成可离线阅读的 HTML。"""
    title = escape(str(row.get("title") or row.get("docId") or "来源文档"))
    blocks = []
    summary = str(row.get("summary") or "").strip()
    if summary:
        blocks.append(f"<p>{escape(summary)}</p>")
    for segment in segments:
        text = str(segment.get("segmentText") or "").strip()
        if text:
            blocks.append(f"<p>{escape(text)}</p>")
    source_url = str(row.get("sourceUrl") or "")
    source_note = (
        f'<p class="meta">原始来源：<a href="{escape(source_url)}">{escape(source_url)}</a>'
        "（当前显示的是数据库离线正文）</p>"
        if source_url else ""
    )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:860px;"
        "margin:40px auto;padding:0 20px;line-height:1.9;color:#1f2937}"
        "h1{font-size:22px;line-height:1.5}.meta{color:#6b7280;font-size:13px}"
        "</style></head><body>"
        f"<h1>{title}</h1>{source_note}{''.join(blocks)}</body></html>"
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.graph_backend not in {"relational", "nebula"}:
            raise ValueError(f"不支持的图存储后端：{settings.graph_backend}")
        db = Database(settings.database_url, settings.seed_dir)
        graph_store = None
        search_index = None
        scheduler = None
        ops_scheduler = None
        try:
            db.initialize()
            if settings.auto_seed:
                db.seed()
            if settings.graph_backend == "nebula":
                graph_store = NebulaGraphStore(db, settings)
                graph_store.initialize()
                graph_store.publish_all()
            snapshot_store = (
                MinioSnapshotStore(
                    settings.minio_endpoint,
                    settings.minio_access_key,
                    settings.minio_secret_key,
                    settings.minio_bucket,
                )
                if settings.minio_endpoint
                else LocalSnapshotStore(settings.data_dir)
            )
            if settings.elasticsearch_url:
                search_index = ElasticsearchSegmentIndex(settings.elasticsearch_url)
            app.state.db = db
            app.state.graph_store = graph_store
            app.state.knowledge = KnowledgeService(db, graph_store)
            app.state.governance = GovernanceService(db, app.state.knowledge)
            app.state.excel_importer = ExcelKnowledgeImporter(db, app.state.knowledge)
            app.state.ingestor = DocumentIngestor(
                db, app.state.knowledge, app.state.excel_importer,
                Path(settings.data_dir) / "uploads",
            )
            app.state.reports = ReportService(db)
            app.state.ontology = OntologyService(db, settings.seed_dir)
            app.state.ontology.initialize()
            app.state.scope = ScopeService(db)
            app.state.scope.initialize()
            app.state.market = MarketService(db, app.state.scope)
            app.state.market.initialize()
            if settings.auto_seed:
                app.state.market.seed_market()
            app.state.policy = PolicyService(db, app.state.scope)
            app.state.conclusion = ConclusionService(db, app.state.policy)
            app.state.pack2b = Pack2bService(db)
            app.state.pack2c = Pack2cService(db)
            app.state.rag = RagService(db, app.state.knowledge, settings)
            try:
                app.state.rag.refresh()
            except Exception:
                pass
            app.state.snapshot_store = snapshot_store
            app.state.archive = ResponseArchive(snapshot_store, settings.data_dir)
            app.state.crawler = Crawler(
                db, app.state.knowledge, snapshot_store, search_index, app.state.archive
            )
            app.state.ops = OpsService(db, app.state.scope, app.state.crawler)
            app.state.ops.initialize()
            ops_scheduler = OpsScheduler(app.state.ops, settings.ops_inspect_interval_minutes)
            if settings.scheduler_enabled:
                ops_scheduler.start()
            scheduler = CrawlScheduler(app.state.crawler, settings.scheduler_timezone)
            app.state.scheduler = scheduler
            if settings.scheduler_enabled:
                scheduler.start()
            yield
        finally:
            if ops_scheduler is not None:
                ops_scheduler.stop()
            if scheduler is not None:
                scheduler.stop()
            if search_index is not None:
                search_index.close()
            if graph_store is not None:
                graph_store.close()
            conclusion = getattr(app.state, "conclusion", None)
            if conclusion is not None:
                conclusion.close()
            db.close()

    app = FastAPI(
        title="粮油产业多维知识图谱系统",
        version="1.0.0",
        description="真实业务实体、关系三元组、来源回溯、质量治理与增量服务",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def knowledge(request: Request) -> KnowledgeService:
        return request.app.state.knowledge

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        service = knowledge(request)
        stats = service.stats()
        return {
            "status": "ok",
            "graphBackend": settings.graph_backend,
            "counts": {
                "knowledgeCategory": stats["categories"],
                "publishedRelations": stats["relations"],
                "entities": stats["entities"],
                "sourceDocuments": stats["documents"],
            },
        }

    @app.post("/api/auth/token")
    def token(form: OAuth2PasswordRequestForm = Depends()) -> dict[str, Any]:
        principal = authenticate(form.username, form.password)
        if not principal:
            from fastapi import HTTPException
            raise HTTPException(401, "用户名或密码错误", {"WWW-Authenticate": "Bearer"})
        return {
            "access_token": issue_token(principal.subject, principal.role, settings.jwt_secret, settings.jwt_expire_minutes),
            "token_type": "bearer",
            "role": principal.role,
        }

    @app.get("/")
    @app.get("/index.html", include_in_schema=False)
    def index() -> FileResponse:
        page = settings.web_dir / "index.html"
        if not page.exists():
            from fastapi import HTTPException
            raise HTTPException(503, "业务图谱页面尚未部署")
        return FileResponse(page)

    @app.get("/graph.html")
    def graph_view() -> FileResponse:
        page = settings.web_dir / "graph.html"
        if not page.exists():
            from fastapi import HTTPException
            raise HTTPException(503, "知识图谱可视化页面尚未部署")
        return FileResponse(page)

    @app.get("/market.html")
    def market_view() -> FileResponse:
        page = settings.web_dir / "market.html"
        if not page.exists():
            raise HTTPException(503, "市场行情工作台尚未部署")
        return FileResponse(page)

    @app.get("/ops.html")
    def ops_view() -> FileResponse:
        page = settings.web_dir / "ops.html"
        if not page.exists():
            raise HTTPException(503, "运营工作台尚未部署")
        return FileResponse(page)

    def market_call(action):
        try:
            return action()
        except MarketError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/market/overview")
    def market_overview(request: Request, asOf: str | None = None,
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.overview(as_of=asOf))

    @app.get("/api/market/series")
    def market_series(request: Request,
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(request.app.state.market.list_series)

    @app.get("/api/market/series/{series_id}")
    def market_series_detail(series_id: str, request: Request, asOf: str | None = None,
                             _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.series_detail(series_id, as_of=asOf))

    @app.get("/api/market/timeline")
    def market_timeline(request: Request,
                        from_period: str | None = Query(default=None, alias="from"),
                        to_period: str | None = Query(default=None, alias="to"),
                        category: str | None = None,
                        asOf: str | None = None,
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(
            lambda: request.app.state.market.timeline(from_period, to_period, category, as_of=asOf)
        )

    @app.get("/api/market/assertions")
    def market_assertions(request: Request, subjectId: str | None = None,
                          asOf: str | None = None,
                          _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.assertions(subjectId, as_of=asOf))

    @app.get("/api/market/transmissions")
    def market_transmissions(request: Request,
                             _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(request.app.state.market.transmission_graph)

    @app.get("/api/market/commodity-forms")
    def market_commodity_forms(request: Request, commodityId: str | None = None,
                               _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.list_forms(commodity_id=commodityId))

    @app.get("/api/market/places")
    def market_places(request: Request, marketType: str | None = None,
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.list_places(market_type=marketType))

    @app.get("/api/market/impacts")
    def market_impacts(request: Request,
                       sourceType: str | None = None,
                       sourceId: str | None = None,
                       targetType: str | None = None,
                       targetId: str | None = None,
                       asOf: str | None = None,
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.list_impacts(
            source_type=sourceType, source_id=sourceId,
            target_type=targetType, target_id=targetId, as_of=asOf,
        ))

    @app.get("/api/market/evidence")
    def market_evidence(request: Request,
                        targetType: str | None = None,
                        targetId: str | None = None,
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.list_evidence(targetType, targetId))

    @app.get("/api/market/factors/{factorId}")
    def market_factor_detail(factorId: str, request: Request, asOf: str | None = None,
                             _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return market_call(lambda: request.app.state.market.factor_detail(factorId, as_of=asOf))

    @app.get("/api/kg/search")
    def search(
        request: Request,
        keyword: str | None = Query(default=None, max_length=100),
        knowledgeType: str | None = None,
        categoryCode: str | None = None,
        industryStage: str | None = None,
        factorDimension: str | None = None,
        commodityCode: str | None = None,
        regionCode: str | None = None,
        dateFrom: str | None = None,
        dateTo: str | None = None,
        pageNo: int = Query(default=1, ge=1),
        pageSize: int = Query(default=20, ge=1, le=100),
        _: Principal = Depends(current_principal),
    ) -> dict[str, Any]:
        return knowledge(request).search(locals() | {"request": None, "_": None})

    @app.get("/api/kg/entities/{entity_type}/{entity_id}")
    def entity_detail(entity_type: str, entity_id: str, request: Request,
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).detail(entity_type, entity_id)

    @app.get("/api/kg/traceability/{entity_type}/{entity_id}")
    def traceability(entity_type: str, entity_id: str, request: Request,
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        row = knowledge(request).require(entity_type, entity_id)
        sources = knowledge(request).sources(entity_type, row)
        if not sources:
            from fastapi import HTTPException
            raise HTTPException(404, "该实体没有可回溯的来源段落")
        return sources[0]

    @app.get("/api/kg/graph/expand")
    def graph_expand(request: Request, entityType: str, entityId: str,
                     depth: int = Query(default=1, ge=1, le=3),
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).graph(entityType, entityId, depth)

    @app.get("/api/kg/taxonomy")
    def taxonomy(request: Request, pageNo: int = Query(default=1, ge=1),
                 pageSize: int = Query(default=100, ge=1, le=500),
                 _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).taxonomy(pageNo, pageSize)

    @app.get("/api/kg/stats")
    def stats(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).stats()

    @app.get("/api/kg/changes")
    def changes(request: Request, since: str, limit: int = Query(default=100, ge=1, le=1000),
                entityType: str | None = None,
                _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).changes(since, limit, entityType)

    @app.post("/api/sources", status_code=201)
    def register_source(payload: dict[str, Any], request: Request,
                        principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        return knowledge(request).register_source(payload, principal.subject)

    @app.post("/api/sources/excel", status_code=201)
    async def upload_excel(
        request: Request,
        file: UploadFile = File(...),
        knowledgeCategoryId: str | None = Form(default=None),
        autoPublish: bool = Form(default=True),
        principal: Principal = Depends(require_roles("ROLE-02")),
    ) -> dict[str, Any]:
        content = await file.read()
        return request.app.state.excel_importer.import_workbook(
            file.filename or "upload.xlsx", content, knowledgeCategoryId, autoPublish, principal.subject
        )

    @app.get("/api/sources/excel/template")
    def download_excel_template(_: Principal = Depends(current_principal)) -> Response:
        return Response(
            content=build_template_workbook(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=\"grain-oil-excel-template.xlsx\""},
        )

    @app.post("/api/sources/upload", status_code=201)
    async def upload_document(
        request: Request,
        file: UploadFile = File(...),
        title: str | None = Form(default=None),
        docType: str | None = Form(default=None),
        issuer: str | None = Form(default=None),
        publishDate: str | None = Form(default=None),
        sourceUrl: str | None = Form(default=None),
        crawlSourceId: str | None = Form(default=None),
        knowledgeCategoryId: str | None = Form(default=None),
        autoPublish: bool = Form(default=False),
        principal: Principal = Depends(require_roles("ROLE-02")),
    ) -> dict[str, Any]:
        content = await file.read()
        meta = {
            "title": title,
            "docType": docType,
            "issuer": issuer,
            "publishDate": publishDate,
            "sourceUrl": sourceUrl,
            "crawlSourceId": crawlSourceId,
            "knowledgeCategoryId": knowledgeCategoryId,
        }
        return request.app.state.ingestor.ingest(
            file.filename or "upload", content, meta, autoPublish, principal.subject
        )

    FILE_MEDIA_TYPES = {
        "PDF": "application/pdf",
        "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "XLSM": "application/vnd.ms-excel.sheet.macroEnabled.12",
        "DOCX": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "CSV": "text/csv; charset=utf-8",
        "TXT": "text/plain; charset=utf-8",
        "MD": "text/markdown; charset=utf-8",
        "HTML": "text/html; charset=utf-8",
    }

    def file_principal(request: Request, token: str | None = Query(None)) -> Principal:
        """文件链接由浏览器新窗口打开，无法附带请求头，因此允许 token 查询参数。"""
        header = request.headers.get("Authorization", "")
        raw = token or (header[7:] if header.lower().startswith("bearer ") else "")
        if not raw:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少访问令牌")
        return principal_from_token(request, raw)

    @app.get("/api/sources/{doc_id}/file")
    def source_file(doc_id: str, request: Request,
                    _: Principal = Depends(file_principal)) -> Response:
        row = request.app.state.db.fetch_one(
            'SELECT "docId","title","checksum","fileFormat","status","storageUri","sourceUrl","summary" '
            'FROM "SourceDoc" WHERE "docId"=:id',
            {"id": doc_id},
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"来源文档不存在：{doc_id}")
        matches = sorted((Path(settings.data_dir) / "uploads").glob(f"{row['checksum']}.*")) \
            if row.get("checksum") else []
        media = FILE_MEDIA_TYPES.get(str(row.get("fileFormat") or "").upper(), "application/octet-stream")
        disposition = "inline" if media.startswith(("text/", "application/pdf")) else "attachment"
        if matches:
            path = matches[0]
            filename = f"{row['title'] or doc_id}{path.suffix}"
            return FileResponse(
                path,
                media_type=media,
                headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"},
            )
        if row.get("checksum"):
            content = request.app.state.snapshot_store.get(row["checksum"])
            if content is not None:
                filename = f"{row['title'] or doc_id}.html"
                return Response(
                    content=content,
                    media_type=media,
                    headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}"},
                )
        # 没有网页原件时，用数据库已保存的正文生成离线页，绝不跳转外部网址
        segments = request.app.state.db.fetch_all(
            'SELECT "segmentText" FROM "SourceDoc_Segment" WHERE "docId"=:doc_id '
            'ORDER BY "segmentNo"', {"doc_id": doc_id},
        )
        filename = f"{row['title'] or doc_id}.html"
        return Response(
            content=offline_document_html(row, segments),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"},
        )

    @app.get("/api/archive/{filename}")
    def archive_asset(filename: str, request: Request,
                      _: Principal = Depends(file_principal)) -> Response:
        parts = request.app.state.archive.split_name(filename)
        if not parts:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "归档资源不存在")
        checksum, extension = parts
        content = request.app.state.archive.get(checksum, extension)
        if content is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "归档资源不存在")
        return Response(
            content=content,
            media_type=guess_media_type(extension),
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}"},
        )

    @app.get("/api/sources/{doc_id}/segments")
    def source_segments(doc_id: str, request: Request,
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
        rows = request.app.state.db.fetch_all(
            'SELECT "segmentId","segmentNo","pageNo","chapterPath","segmentText" '
            'FROM "SourceDoc_Segment" WHERE "docId"=:doc_id ORDER BY "segmentNo"',
            {"doc_id": doc_id},
        )
        return {"total": len(rows), "items": rows}

    @app.post("/api/sources/{doc_id}/void")
    def void_source(doc_id: str, request: Request,
                    principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        result = request.app.state.governance.void_source(doc_id, principal.subject)
        try:
            result["rag"] = request.app.state.rag.remove_document(doc_id)
        except Exception as exc:
            result["rag"] = {"removed": 0, "error": str(exc)}
        return result

    @app.post("/api/sources/{doc_id}/restore")
    def restore_source(doc_id: str, request: Request,
                       principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        result = request.app.state.governance.restore_source(doc_id, principal.subject)
        try:
            request.app.state.rag.refresh()
            result["rag"] = request.app.state.rag.status()
        except Exception as exc:
            result["rag"] = {"error": str(exc)}
        return result

    @app.post("/api/knowledge/{entity_type}", status_code=201)
    def create_knowledge(entity_type: str, payload: dict[str, Any], request: Request,
                         principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        return knowledge(request).create_knowledge(entity_type, payload, principal.subject)

    @app.post("/api/knowledge/{entity_type}/{entity_id}/submit")
    def submit_knowledge(entity_type: str, entity_id: str, request: Request,
                         principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        return knowledge(request).submit(entity_type, entity_id, principal.subject)

    @app.post("/api/knowledge/{entity_type}/{entity_id}/review")
    def review_knowledge(entity_type: str, entity_id: str, payload: dict[str, Any], request: Request,
                         principal: Principal = Depends(require_roles("ROLE-03"))) -> dict[str, Any]:
        return knowledge(request).review(entity_type, entity_id, payload.get("decision", ""), principal.subject)

    @app.post("/api/master/{entity_type}", status_code=201)
    def create_master(entity_type: str, payload: dict[str, Any], request: Request,
                      principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return knowledge(request).create_master(entity_type, payload, principal.subject)

    @app.get("/api/master/{entity_type}")
    def list_master(entity_type: str, request: Request,
                    keyword: str | None = None, limit: int = Query(default=500, ge=1, le=2000),
                    _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return {"items": knowledge(request).list_entities(entity_type, keyword, limit)}

    @app.get("/api/entities/{entity_type}")
    def list_entities(entity_type: str, request: Request,
                      keyword: str | None = None,
                      limit: int = Query(default=500, ge=1, le=2000),
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return {"items": knowledge(request).list_entities(entity_type, keyword, limit)}

    @app.get("/api/knowledge/{entity_type}")
    def list_knowledge(entity_type: str, request: Request,
                       status: str | None = None, keyword: str | None = None,
                       pageNo: int = Query(default=1, ge=1),
                       pageSize: int = Query(default=20, ge=1, le=200),
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).list_knowledge(entity_type, status, keyword, pageNo, pageSize)

    @app.get("/api/review/tasks")
    def review_tasks(request: Request, status: str = "待审核",
                     entityType: str | None = None, keyword: str | None = None,
                     pageNo: int = Query(default=1, ge=1),
                     pageSize: int = Query(default=50, ge=1, le=200),
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return knowledge(request).list_tasks(status, entityType, keyword, pageNo, pageSize)

    @app.post("/api/governance/detect")
    def detect_quality(request: Request,
                       principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.governance.detect(principal.subject)

    @app.get("/api/governance/records")
    def quality_records(request: Request, issueType: str | None = None,
                        _: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.governance.records(issueType)

    @app.post("/api/governance/merge")
    def merge_entities(payload: dict[str, Any], request: Request,
                       principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.governance.merge(
            payload.get("entityType", ""), payload.get("sourceEntityId", ""),
            payload.get("targetEntityId", ""), float(payload.get("similarity", 1)), principal.subject,
        )

    @app.post("/api/crawl/trigger")
    def trigger_crawl(payload: dict[str, Any], request: Request,
                      principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        return request.app.state.crawler.trigger(payload.get("crawlSourceId", ""), principal.subject)

    @app.post("/api/crawl/run-all")
    def crawl_run_all(payload: dict[str, Any] | None, request: Request,
                      principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        """逐域触发所有启用的采集源，执行结构化抽取并导入。"""
        crawler = request.app.state.crawler
        db = request.app.state.db
        payload = payload or {}
        extractor_order = ["policy_gov", "market_index_moa", "yearbook_stats",
                           "intel_agri", "relation_derive"]
        only = payload.get("extractorTypes") or extractor_order
        sources = db.fetch_all(
            'SELECT * FROM "CrsSource" WHERE "enabledFlag"=:enabled ORDER BY "crawlSourceId"',
            {"enabled": "启用"},
        )
        ordered = sorted(
            [s for s in sources if s.get("extractorType") in only],
            key=lambda s: only.index(s["extractorType"]) if s.get("extractorType") in only else 999,
        )
        results = []
        for source in ordered:
            try:
                res = crawler.trigger(source["crawlSourceId"], principal.subject)
                results.append({"crawlSourceId": source["crawlSourceId"],
                                "extractorType": source.get("extractorType"), **res})
            except Exception as exc:
                results.append({"crawlSourceId": source["crawlSourceId"],
                                "extractorType": source.get("extractorType"),
                                "runStatus": "失败", "error": str(exc)[:500]})
        return {"total": len(ordered), "results": results}

    @app.get("/api/crawl/sources")
    def crawl_sources(request: Request,
                      _: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        rows = request.app.state.db.fetch_all('SELECT * FROM "CrsSource" ORDER BY "crawlSourceId"')
        return {"total": len(rows), "items": rows}

    @app.post("/api/crawl/sources", status_code=201)
    def create_crawl_source(payload: dict[str, Any], request: Request,
                            principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        db = request.app.state.db
        row = dict(payload)
        row["crawlSourceId"] = db.next_id("CrsSource", "CRS")
        row.setdefault("crawlMode", "手动")
        row.setdefault("minIntervalMinutes", 60)
        row.setdefault("maxRetry", 3)
        row.setdefault("enabledFlag", "启用")
        missing = [
            column for column in ("sourceName", "sourceUrl", "sourceType", "contentCategory",
                                  "knowledgeCategoryId")
            if not row.get(column)
        ]
        if missing:
            from fastapi import HTTPException
            raise HTTPException(422, f"缺少必填字段：{', '.join(missing)}")
        knowledge(request).require("知识分类", row["knowledgeCategoryId"])
        with db.engine.begin() as conn:
            db.insert("CrsSource", row, conn, actor=principal.subject)
        return {"crawlSourceId": row["crawlSourceId"], "enabledFlag": row["enabledFlag"]}

    @app.put("/api/crawl/sources/{source_id}")
    def update_crawl_source(source_id: str, payload: dict[str, Any], request: Request,
                            principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        db = request.app.state.db
        existing = db.fetch_one(
            'SELECT * FROM "CrsSource" WHERE "crawlSourceId"=:id', {"id": source_id}
        )
        if not existing:
            from fastapi import HTTPException
            raise HTTPException(404, f"采集源不存在：{source_id}")
        values = {key: value for key, value in payload.items() if key != "crawlSourceId"}
        with db.engine.begin() as conn:
            db.update("CrsSource", source_id, values, conn, actor=principal.subject)
        return {"crawlSourceId": source_id, "updated": list(values)}

    @app.post("/api/crawl/sources/{source_id}/toggle")
    def toggle_crawl_source(source_id: str, request: Request,
                            principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        db = request.app.state.db
        existing = db.fetch_one(
            'SELECT * FROM "CrsSource" WHERE "crawlSourceId"=:id', {"id": source_id}
        )
        if not existing:
            from fastapi import HTTPException
            raise HTTPException(404, f"采集源不存在：{source_id}")
        enabled = "停用" if existing.get("enabledFlag") == "启用" else "启用"
        with db.engine.begin() as conn:
            db.update("CrsSource", source_id, {"enabledFlag": enabled}, conn, actor=principal.subject)
        return {"crawlSourceId": source_id, "enabledFlag": enabled}

    @app.get("/api/crawl/runs")
    def crawl_runs(request: Request, limit: int = Query(default=50, ge=1, le=500),
                   _: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        rows = request.app.state.db.fetch_all(
            'SELECT * FROM "CrsSource_CrawlRun" ORDER BY "startedAt" DESC LIMIT :limit',
            {"limit": limit},
        )
        return {"total": len(rows), "items": rows}

    @app.get("/api/reports/{code}")
    def report(code: str, request: Request, since: str | None = None,
               _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.reports.get(code, since)

    @app.post("/api/policies/judge")
    def policies_judge(payload: dict[str, Any], request: Request,
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        from fastapi import HTTPException
        try:
            result = request.app.state.conclusion.judge(
                facts=dict(payload.get("facts") or {}),
                as_of=payload.get("asOf"),
                scope_id=payload.get("scopeId"),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if payload.get("asOf"):
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        return result

    @app.post("/api/policy/judge")
    def policy_judge(payload: dict[str, Any], request: Request,
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        from fastapi import HTTPException
        as_of = payload.get("asOf")
        max_hops = payload.get("maxHops", 3)
        if not isinstance(as_of, str) or not as_of.strip():
            raise HTTPException(422, "asOf 必填，且必须是 YYYY-MM-DD 日期")
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or not 1 <= max_hops <= 6:
            raise HTTPException(422, "maxHops 必须是 1–6 的整数")
        try:
            result = request.app.state.conclusion.judge(
                facts=dict(payload.get("facts") or {}),
                as_of=as_of,
                max_hops=max_hops,
                formal_only=True,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(
            {"outcomes": result["verdicts"], "asOf": result["asOf"]},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/policies/extract-2b")
    def pack2b_extract(payload: dict[str, Any], request: Request,
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        try:
            return request.app.state.pack2b.extract(payload.get("policyId"))
        except ProtectedAssetError as exc:
            raise HTTPException(409, {"message": str(exc), "protectedIds": exc.protected_ids}) from exc

    @app.post("/api/policies/compile-2c")
    def pack2c_compile(payload: dict[str, Any], request: Request,
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        try:
            return request.app.state.pack2c.compile(payload.get("policyId"))
        except ProtectedAssetError as exc:
            raise HTTPException(409, {"message": str(exc), "protectedIds": exc.protected_ids}) from exc

    @app.post("/api/rag/query")
    def rag_query(payload: dict[str, Any], request: Request,
                  _: Principal = Depends(current_principal)) -> dict[str, Any]:
        from fastapi import HTTPException
        try:
            # 政策问答的时效与范围口径统一走 PolicyService，检索层只负责把结果讲进答案
            policies = request.app.state.policy.effective(
                payload.get("asOf"), 5, payload.get("scopeId")
            )
            top_k = payload.get("topK", 10)
            if top_k is None or top_k == "":
                top_k = 10
            return request.app.state.rag.search(
                str(payload.get("query") or ""),
                str(payload.get("mode") or "hybrid"),
                int(top_k),
                bool(payload.get("answer", True)),
                policies,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/rag/status")
    def rag_status(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.rag.status()

    @app.post("/api/rag/rebuild")
    def rag_rebuild(request: Request,
                    _: Principal = Depends(require_roles("ROLE-01", "ROLE-02"))) -> dict[str, Any]:
        return request.app.state.rag.rebuild()

    @app.get("/api/rag/config")
    def rag_config(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.rag.public_config()

    @app.post("/api/rag/config")
    def rag_configure(payload: dict[str, Any], request: Request,
                      _: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        from fastapi import HTTPException
        try:
            return request.app.state.rag.configure(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/ontology/stats")
    def ontology_stats(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ontology.stats()

    @app.get("/api/ontology/object-types")
    def ontology_object_types(request: Request, keyword: str | None = None,
                              _: Principal = Depends(current_principal)) -> dict[str, Any]:
        items = request.app.state.ontology.object_types(keyword)
        return {"total": len(items), "items": items}

    @app.get("/api/ontology/object-types/{type_id}")
    def ontology_object_type(type_id: str, request: Request,
                             _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ontology.object_type(type_id)

    @app.get("/api/ontology/object-types/{type_id}/objects")
    def ontology_objects(type_id: str, request: Request, keyword: str | None = None,
                         pageNo: int = 1, pageSize: int = 20,
                         _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ontology.objects(type_id, keyword, pageNo, pageSize)

    @app.get("/api/ontology/link-types")
    def ontology_link_types(request: Request,
                            _: Principal = Depends(current_principal)) -> dict[str, Any]:
        items = request.app.state.ontology.link_types()
        return {"total": len(items), "items": items}

    @app.get("/api/ontology/action-types")
    def ontology_action_types(request: Request,
                              _: Principal = Depends(current_principal)) -> dict[str, Any]:
        items = request.app.state.ontology.action_types()
        return {"total": len(items), "items": items}

    @app.get("/api/ontology/dimensions")
    def ontology_dimensions(request: Request,
                            _: Principal = Depends(current_principal)) -> dict[str, Any]:
        items = request.app.state.scope.dimensions()
        return {"total": len(items), "items": items}

    @app.get("/api/ontology/scopes")
    def ontology_scopes(request: Request, scopeType: str | None = None, limit: int = 200,
                        _: Principal = Depends(current_principal)) -> dict[str, Any]:
        items = request.app.state.scope.scopes(scopeType, limit)
        return {"total": len(items), "items": items}

    @app.get("/api/ontology/scopes/{scope_id}")
    def ontology_scope(scope_id: str, request: Request,
                       _: Principal = Depends(current_principal)) -> dict[str, Any]:
        item = request.app.state.scope.scope(scope_id)
        if not item:
            raise HTTPException(404, "范围不存在")
        return item

    @app.post("/api/ontology/scopes", status_code=status.HTTP_201_CREATED)
    def ontology_scope_create(payload: dict[str, Any], request: Request,
                              principal: Principal = Depends(require_roles("ROLE-01", "ROLE-02"))) -> dict[str, Any]:
        try:
            return request.app.state.scope.upsert_scope(
                scope_type=payload.get("scopeType"),
                members=payload.get("members") or [],
                scope_code=payload.get("scopeCode"),
                label=payload.get("label"),
                is_open=bool(payload.get("isOpen")),
                superset_of=payload.get("supersetOf") or [],
                actor=principal.subject,
            )
        except ScopeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/policies/effective")
    def effective_policies(request: Request, asOf: str | None = None,
                           scopeId: str | None = None,
                           limit: int = Query(default=200, ge=1, le=1000),
                           _: Principal = Depends(current_principal)) -> Any:
        try:
            result = request.app.state.policy.effective(asOf, limit, scopeId)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # 指定时点等于一次历史回溯，不能被中间层缓存成「当前」结果
        if asOf:
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        return result

    @app.get("/api/ontology/graph")
    def ontology_graph(request: Request, _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ontology.graph()

    @app.post("/api/ontology/refresh")
    def ontology_refresh(request: Request,
                         _: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ontology.sync()

    @app.get("/api/ops/overview")
    def ops_overview(request: Request,
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ops.overview()

    @app.get("/api/ops/ingest")
    def ops_ingest(request: Request,
                   _: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        return request.app.state.ops.ingest()

    @app.post("/api/ops/ingest/retry")
    def ops_ingest_retry(payload: dict[str, Any], request: Request,
                         principal: Principal = Depends(require_roles("ROLE-02"))) -> dict[str, Any]:
        source_id = str(payload.get("crawlSourceId") or "").strip()
        if not source_id:
            raise HTTPException(422, "crawlSourceId 不能为空")
        return request.app.state.ops.retry_failed(source_id, principal.subject)

    @app.post("/api/ops/inspect")
    def ops_inspect(request: Request,
                    principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.inspect(principal.subject)

    @app.get("/api/ops/metrics")
    def ops_metrics(request: Request, metricCode: str | None = None,
                    limit: int = Query(default=100, ge=1, le=500),
                    _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ops.metrics(metricCode, limit)

    @app.get("/api/ops/alerts")
    def ops_alerts(request: Request, status: str | None = None,
                   limit: int = Query(default=100, ge=1, le=500),
                   _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ops.alerts(status, limit)

    @app.post("/api/ops/alerts/{alert_id}/ack")
    def ops_alert_ack(alert_id: str, request: Request,
                      principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.ack(alert_id, principal.subject)

    @app.post("/api/ops/alerts/{alert_id}/close")
    def ops_alert_close(alert_id: str, request: Request,
                        principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.close(alert_id, principal.subject)

    @app.get("/api/ops/evolution")
    def ops_evolution(request: Request, status: str | None = None,
                      targetKind: str | None = None,
                      _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ops.list_changes(status, targetKind)

    @app.post("/api/ops/evolution")
    def ops_evolution_create(payload: dict[str, Any], request: Request,
                             principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.create_draft(payload, principal.subject)

    @app.post("/api/ops/evolution/{change_id}/submit")
    def ops_evolution_submit(change_id: str, request: Request,
                             principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.submit(change_id, principal.subject)

    @app.post("/api/ops/evolution/{change_id}/review")
    def ops_evolution_review(change_id: str, payload: dict[str, Any], request: Request,
                             principal: Principal = Depends(require_roles("ROLE-03"))) -> dict[str, Any]:
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise HTTPException(422, "approved 必须为布尔值")
        return request.app.state.ops.review(
            change_id, approved, payload.get("remark"), principal.subject
        )

    @app.post("/api/ops/evolution/{change_id}/publish")
    def ops_evolution_publish(change_id: str, request: Request,
                              principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.publish(change_id, principal.subject)

    @app.post("/api/ops/evolution/{change_id}/rollback")
    def ops_evolution_rollback(change_id: str, request: Request,
                               principal: Principal = Depends(require_roles("ROLE-01"))) -> dict[str, Any]:
        return request.app.state.ops.rollback(change_id, principal.subject)

    @app.get("/api/ops/releases")
    def ops_releases(request: Request,
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
        return request.app.state.ops.list_releases()

    return app


app = create_app()
