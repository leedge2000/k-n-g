import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

from app.yearbook import GRID_X0, GRID_STEP, group_rows, ocr_image

work = Path("data/_tmp/work2")
payload = json.loads(next(Path("data/yearbook_parsed").glob("*.json")).read_text(encoding="utf-8"))["parts"][0]
centers = {}
for words in group_rows(payload):
    text = "".join(w["text"] for w in words)
    for token in ("1978", "1985", "202"):
        if text.startswith(token):
            centers[token] = [w["y"] + w["h"] / 2 for w in words]
print({k: sum(v) / len(v) for k, v in centers.items()})

img = Image.open(work / "part0.png")
print("part0 size", img.size, img.mode)
for label in ("1978", "202"):
    if label not in centers:
        continue
    y = int(sum(centers[label]) / len(centers[label]))
    strip = img.crop((0, max(0, y - 150), 3000, min(img.height, y + 150)))
    path = work / f"row_{label}.png"
    strip.save(path)
    try:
        data = ocr_image(path)
        print(label, strip.size, "->", " | ".join("".join(w["text"] for w in line["words"]) for line in data["lines"])[:300])
    except RuntimeError as exc:
        print(label, strip.size, "FAIL", str(exc)[:120])
