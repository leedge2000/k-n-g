import sys
from pathlib import Path

sys.path.insert(0, r"c:\粮油知识图谱\kg-system")
from app import yearbook

work = Path(r"c:\粮油知识图谱\kg-system\data\_tmp\ocr_work")
payload = yearbook.ocr_image(work / "part0.png")

rows = yearbook.group_rows(payload)
header = []
for words in rows:
    text = "".join(w["text"] for w in words)
    first_y = min(w["y"] for w in words)
    if first_y < 110:
        header.append([(w["text"], round(w["x"] + w["w"] / 2)) for w in words])

print("== 表头词与 x 中心 ==")
for h in header:
    print([f"{t}@{x}" for t, x in h])

print("\n== 数据行 cell 与 x 中心 ==")
for words in rows:
    cells = yearbook.split_cells(words)
    if not cells:
        continue
    y0 = "".join(cells[0]).strip()
    if y0 not in {"1978", "1995", "2010", "2023"}:
        continue
    centers = []
    cursor = None
    for w in words:
        if cursor is None or w["x"] - cursor >= yearbook.CELL_GAP:
            centers.append([round(w["x"] + w["w"] / 2), w["text"]])
        else:
            centers[-1][1] += w["text"]
        cursor = w["x"] + w["w"]
    print(y0, "cells=", len(cells))
    print("   ", [f"{t}@{x}" for x, t in centers])
