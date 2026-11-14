import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.yearbook import parse_image

start = time.time()
parsed = parse_image(Path("data/yearbook_imgs/C12-10.jpg"), "12-10", Path("data/yearbook_parsed"))
print(f"elapsed={time.time()-start:.1f}s sha={parsed.image_sha256[:12]}")
print(f"rows={len(parsed.rows)} rejected={len(parsed.rejected)}")
for row in parsed.rows:
    v = row["values"]
    print(row["year"], v.get("粮食"), v.get("稻谷"), v.get("小麦"), v.get("玉米"), v.get("大豆"))
print("--rejected--")
for item in parsed.rejected:
    print(item)
