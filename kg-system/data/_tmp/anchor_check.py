import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.yearbook import group_rows, CELL_GAP


def cell_spans(words, min_gap=CELL_GAP):
    spans = []
    cursor = None
    for w in words:
        if cursor is None or w["x"] - cursor >= min_gap:
            spans.append({"x0": w["x"], "x1": w["x"] + w["w"], "tokens": [w["text"]]})
        else:
            spans[-1]["tokens"].append(w["text"])
            spans[-1]["x1"] = w["x"] + w["w"]
        cursor = w["x"] + w["w"]
    return spans


payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
scale = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
wanted = {"1978", "1995", "2010", "2023"}
for words in group_rows(payload):
    cells = cell_spans(words)
    if not cells:
        continue
    year = "".join(cells[0]["tokens"]).strip()
    if year not in wanted:
        continue
    out = []
    for c in cells[1:]:
        cx = (c["x0"] + c["x1"]) / 2 / scale
        out.append(f"{cx:.0f}:{''.join(c['tokens'])}")
    print(f"{year} n={len(cells)-1} " + " | ".join(out))
