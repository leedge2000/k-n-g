import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

work = Path("data/_tmp/work2")
img = Image.open(work / "part0.png")
cell = img.crop((140, 2925, 560, 3035))
cell = cell.resize((cell.width * 4, cell.height * 4), Image.LANCZOS)
cell.save(work / "cell2024b.png")
year = img.crop((0, 2925, 200, 3035))
year = year.resize((year.width * 4, year.height * 4), Image.LANCZOS)
year.save(work / "year2024.png")
print("ok", cell.size, year.size)
