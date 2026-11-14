import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.yearbook import GRID_X0, GRID_STEP, group_rows

payload = json.loads(Path("data/_ocr_p0.json").read_text(encoding="utf-8-sig"))
for words in group_rows(payload):
    buckets: dict[int, list[str]] = {}
    for w in words:
        idx = round(((w["x"] + w["w"] / 2) - GRID_X0) / GRID_STEP)
        buckets.setdefault(idx, []).append(w["text"])
    first = "".join(buckets.get(-1, []))
    zeros = "".join(buckets.get(0, []))
    if not first and not zeros:
        continue
    print(f"year_cell={first!r:12} col0={zeros!r}")
