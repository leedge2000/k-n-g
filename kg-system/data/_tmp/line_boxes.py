import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

from app.yearbook import ocr_image

work = Path("data/_tmp/work2")
img = Image.open(work / "part0.png")

strip = img.crop((150, 2850, 1800, 3160))
path = work / "zoom_rows.png"
strip.save(path)
data = ocr_image(path)
for line in data["lines"]:
    boxes = [(w["text"], round(w["x"]), round(w["y"] + w["h"] / 2)) for w in line["words"]]
    print(boxes)
