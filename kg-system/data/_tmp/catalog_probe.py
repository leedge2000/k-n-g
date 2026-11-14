import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from app.yearbook import parse_catalog

url = "https://www.stats.gov.cn/sj/ndsj/2025/left.htm"
r = httpx.get(url, headers={"User-Agent": "GrainOilKnowledgeGraph/1.0"}, timeout=20, follow_redirects=True)
text = r.content.decode("gb18030", "ignore")
entries = parse_catalog(text)
print("catalog", len(entries))
for e in entries:
    if e.table_no.startswith("12-"):
        print(e.table_no, e.title, e.image_url)
img = "https://www.stats.gov.cn/sj/ndsj/2025/html/C12-10.jpg"
ir = httpx.get(img, headers={"User-Agent": "GrainOilKnowledgeGraph/1.0"}, timeout=30, follow_redirects=True)
print("img", ir.status_code, len(ir.content), ir.headers.get("content-type"))
