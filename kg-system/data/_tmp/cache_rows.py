import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.yearbook import GRID_X0, GRID_STEP, group_rows

cache = Path("data/yearbook_parsed/9f74dac21719").with_suffix(".json")
if not cache.exists():
    cache = list(Path("data/yearbook_parsed").glob("*"))[0]
print("cache:", cache)
data = json.loads(cache.read_text(encoding="utf-8"))
print("parts:", len(data.get("parts", [])))
for i, payload in enumerate(data.get("parts", [])):
    print(f"== part {i} ==")
    for words in group_rows(payload):
        buckets: dict[int, list[str]] = {}
        for w in words:
            idx = round(((w["x"] + w["w"] / 2) - GRID_X0) / GRID_STEP)
            buckets.setdefault(idx, []).append(w["text"])
        first = "".join(buckets.get(-1, []))
        zeros = "".join(buckets.get(0, []))
        if not first and not zeros:
            continue
        print(f"year={first!r:12} col0={zeros!r}")
