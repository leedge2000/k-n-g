import sys
from pathlib import Path

sys.path.insert(0, r"c:\粮油知识图谱\kg-system")
from app import yearbook

src = Path(r"c:\粮油知识图谱\kg-system\data\yearbook_imgs\C12-10.jpg")
work = Path(r"c:\粮油知识图谱\kg-system\data\_tmp\ocr_work")
parts = yearbook.prepare_images(src, work)
print("parts", [p.name for p in parts], [p.stat().st_size for p in parts])

all_rows = []
rejected = []
for p in parts:
    payload = yearbook.ocr_image(p)
    rows, bad = yearbook.parse_table("12-10", payload)
    print(p.name, "lines", payload.get("lineCount"), "rows", len(rows), "bad", bad[:3])
    all_rows.extend(rows)
    rejected.extend(bad)

print("total raw rows", len(all_rows))
for r in sorted(all_rows, key=lambda x: x["year"]):
    v = r["values"]
    print(r["year"], "w=", r["width"], "粮食=", v.get("粮食"), "稻谷=", v.get("稻谷"),
          "小麦=", v.get("小麦"), "玉米=", v.get("玉米"), "大豆=", v.get("大豆"),
          "油料=", v.get("油料"))

kept, notes = yearbook.validate_rows(all_rows)
print("kept", len(kept), "notes", notes[:10])
