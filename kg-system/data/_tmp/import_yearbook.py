"""年鉴域真实数据导入：更新采集源指向年鉴目录页，触发图像表格抽取。"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.crawler import Crawler
from app.db import Database

CATALOG_URL = "https://www.stats.gov.cn/sj/ndsj/2025/left.htm"


def main() -> None:
    settings = Settings.from_env()
    db_path = Path(settings.database_url.replace("sqlite:///", ""))
    backup_dir = Path(settings.data_dir) / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{db_path.stem}_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}"
    shutil.copy2(db_path, backup)
    print("backup:", backup)

    db = Database(settings.database_url, settings.seed_dir)
    source = db.fetch_one('SELECT * FROM "CrsSource" WHERE "crawlSourceId"=:id', {"id": "CRS0007"})
    if not source:
        raise SystemExit("CRS0007 不存在")
    print("before:", source["sourceUrl"], source["extractorType"])
    with db.engine.begin() as conn:
        db.update("CrsSource", "CRS0007", {"sourceUrl": CATALOG_URL, "charset": "GB18030"},
                  conn, actor="yearbook-import")
    print("after:", db.fetch_one('SELECT "sourceUrl","charset" FROM "CrsSource" WHERE "crawlSourceId"=\'CRS0007\''))

    before_count = db.fetch_one('SELECT COUNT(*) AS n FROM "YrbYearbook"')["n"]
    crawler = Crawler(db)
    result = crawler.trigger("CRS0007", "yearbook-import")
    print("run:", result)
    after_count = db.fetch_one('SELECT COUNT(*) AS n FROM "YrbYearbook"')["n"]
    print(f"YrbYearbook {before_count} -> {after_count}")
    for row in db.fetch_all(
        'SELECT "statYear","tableName","value","unit","commodityId","caliberNote" FROM "YrbYearbook" '
        'WHERE "commodityId" IS NOT NULL ORDER BY "statYear" DESC LIMIT 8'
    ):
        print(dict(row))


if __name__ == "__main__":
    main()
