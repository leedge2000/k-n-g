# 粮油经济本体知识图谱系统

面向粮油产业的经济知识图谱系统：把品种、产品、政策法规与政策条款、地区、指标数据、年鉴记录、农业情报、来源文档与知识分类组织成可推理、可追溯的本体网络，并提供业务图谱、市场行情、综合知识图谱、运营工作台四个前端入口。

## 功能概览

| 模块 | 页面 | 说明 |
| --- | --- | --- |
| 业务图谱 | `web/index.html` | 品种—产品—地区—指标的业务主视图，支持实体检索、属性与来源证据回溯 |
| 市场行情 | `web/market.html` | 粮油品种现货/期货行情、涨跌与区间走势 |
| 综合知识图谱 | `web/graph.html` | 跨域实体力导向视图，支持层级展开、节点属性、一跳关系与原文来源跳转 |
| 运营工作台 | `web/ops.html` | 采集源健康、增量入库、质量告警、本体与规则演进 |

## 目录结构

```
kg-system/
  app/            FastAPI 后端（图谱、政策、市场、年鉴、RAG、运营、爬虫抽取器）
  web/            前端静态页面（无构建步骤，原生 JS + D3）
  seed/           种子数据
  ontology_ly/    本体资产（kernel 内核、terms 术语、rules 规则）
  tests/          pytest 用例
  data/           运行期数据：SQLite 库、上传原文、Kuzu 图库、备份（已随仓库提供）
  deploy/         Docker 部署（Compose、Nginx、Nebula schema）
```

## 方式一：本地运行（SQLite，零外部依赖）

要求 Python 3.11 及以上。

```powershell
cd kg-system
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium   # 仅“年鉴网页截图解析”需要，可跳过
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>：

- 登录账号：`admin / admin123`（管理员）、`reviewer / reviewer123`（审核员）
- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

默认使用 `kg-system/data/grain_oil.db`（SQLite），图存储后端为 `relational`，无需安装 NebulaGraph。

## 方式二：Docker Compose（生产形态）

包含 PostgreSQL、NebulaGraph 集群、Elasticsearch、MinIO、双 API 实例与 Nginx。

项目自建的两个镜像已发布到 GitHub Container Registry，可直接拉取，无需本地构建：

| 镜像 | 说明 |
| --- | --- |
| `ghcr.io/leedge2000/k-n-g/api:latest` | API 服务（FastAPI） |
| `ghcr.io/leedge2000/k-n-g/minio:RELEASE.2025-04-22T22-12-26Z` | MinIO 对象存储（已内置二进制，免去 GitHub Release 下载） |

```powershell
cd kg-system/deploy
Copy-Item .env.example .env    # 首次部署时按需修改口令
docker compose pull            # 拉取自建镜像
docker compose up -d           # 若本地无镜像且未 pull，compose 会自动构建
```

访问 <http://localhost:8991>（端口由 `.env` 中 `HTTP_PORT` 控制，默认 8991）。

其余依赖（PostgreSQL、NebulaGraph、Elasticsearch、nebula-console）直接使用官方公共镜像，见 `deploy/docker-compose.yml`。

## 数据说明

- `kg-system/data/grain_oil.db` 已随仓库提供，内含已导入的真实数据（政策法规与条款、市场行情、年鉴记录、农业情报、来源文档与关系边），克隆后无需重新采集即可直接浏览。
- 若删除该库，首次启动会按 `KG_AUTO_SEED=true` 从 `seed/` 重建基础数据；由爬虫采集入库的真实数据需重新执行采集，不会自动恢复。
- 上传原文、Kuzu 图库、库备份均位于 `kg-system/data/` 下。

## 主要环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KG_DATABASE_URL` | `sqlite:///kg-system/data/grain_oil.db` | 数据库连接串，可换 PostgreSQL |
| `KG_JWT_SECRET` | 本地开发默认值 | 签发 token 的密钥，生产必须替换 |
| `KG_GRAPH_BACKEND` | `relational` | 图存储后端，可选 `nebula` |
| `KG_AUTO_SEED` | `true` | 启动时是否写入种子数据 |
| `KG_SCHEDULER_ENABLED` | `false` | 是否启用定时采集与巡检 |
| `KG_RAG_EMBEDDING_BACKEND` | `hash` | 向量化后端，可选外部 embedding 服务 |
| `KG_RAG_LLM_BASE_URL` / `KG_RAG_LLM_API_KEY` | 空 | 配置后启用大模型问答增强 |

## 测试

```powershell
cd kg-system
python -m pytest -q
```
