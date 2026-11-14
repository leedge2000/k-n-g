import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

from app.yearbook import GRID_STEP, GRID_X0, group_rows, ocr_image

work = Path("data/_tmp/work4")
work.mkdir(parents=True, exist_ok=True)
src = Image.open("data/yearbook_imgs/C12-10.jpg").convert("L")
w, h = src.size
scale = 4
slices = max(1, round(h * scale / 3400))
step = h // slices
print("src", src.size, "slices", slices)
parts = []
for i in range(slices):
    top = i * step
    bottom = h if i == slices - 1 else (i + 1) * step
    tile = src.crop((0, top, w, bottom)).resize((w * scale, (bottom - top) * scale), Image.LANCZOS)
    path = work / f"part{i}.png"
    tile.save(path)
    parts.append(path)

payload = ocr_image(parts[0])
print("size", payload["width"], payload["height"])
for words in group_rows(payload):
    buckets: dict[int, list[str]] = {}
    for word in words:
        idx = round(((word["x"] + word["w"] / 2) - GRID_X0) / GRID_STEP)
        buckets.setdefault(idx, []).append(word["text"])
    year = "".join(buckets.get(-1, []))
    col0 = "".join(buckets.get(0, []))
    if year or col0:
        print(f"year={year!r:10} col0={col0!r}")
