import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.yearbook import parse_table, validate_rows

payload = json.loads(Path("data/_ocr_p0.json").read_text(encoding="utf-8-sig"))
rows, rejected = parse_table("12-10", payload)
kept, notes = validate_rows(rows)
print(f"parsed={len(rows)} kept={len(kept)} rejected={len(rejected)} notes={len(notes)}")
for row in kept:
    v = row["values"]
    print(row["year"], "粮食", v.get("粮食"), "稻谷", v.get("稻谷"), "小麦", v.get("小麦"),
          "玉米", v.get("玉米"), "大豆", v.get("大豆"), "油料", v.get("油料"))
print("--rejected--")
for item in rejected:
    print(item)
print("--notes--")
for item in notes:
    print(item)
missing = [y for y in (1978, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2023, 2024)
           if y not in {r["year"] for r in kept}]
print("missing years:", missing)
