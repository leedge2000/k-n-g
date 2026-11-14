import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

from app.yearbook import GRID_X0, GRID_STEP, group_rows, ocr_image, prepare_images

src = Path("data/yearbook_imgs/C12-10.jpg")
work = Path("data/_tmp/work2")
work.mkdir(parents=True, exist_ok=True)
parts = prepare_images(src, work)
print("parts:", parts)

cache = json.loads(next(Path("data/yearbook_parsed").glob("*.json")).read_text(encoding="utf-8"))
payload = cache["parts"][0]
target = None
for words in group_rows(payload):
    text = "".join(w["text"] for w in words)
    if ("709" in text or "70" in text[:4]) and "1978" not in text:
        ys = [w["y"] + w["h"] / 2 for w in words]
        if ys:
            target = min(ys)
print("row y center in part0:", target)

img = Image.open(parts[0])
for scale in (1, 2):
    for pad in (120, 240):
        y0 = max(0, int(target - pad))
        y1 = min(img.height, int(target + pad))
        strip = img.crop((0, y0, 2400, y1))
        strip = strip.resize((strip.width * scale // 1, strip.height * scale // 1), Image.LANCZOS)
        path = work / f"strip_{scale}_{pad}.png"
        strip.save(path)
        try:
            data = ocr_image(path)
        except RuntimeError as exc:
            print(scale, pad, "FAIL", exc)
            continue
        text = " ".join("".join(w["text"] for w in line["words"]) for line in data["lines"])
        print(scale, pad, "->", text[:300])
