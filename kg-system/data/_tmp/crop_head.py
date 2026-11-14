import os
from PIL import Image

src = r"c:\粮油知识图谱\kg-system\data\yearbook_imgs\C12-10.jpg"
out = r"c:\粮油知识图谱\kg-system\data\_tmp"
os.makedirs(out, exist_ok=True)
im = Image.open(src)
w, h = im.size
# 表头区域：原图 y 约 95-160，左半部分
head = im.crop((0, 90, 1250, 165)).resize((1250 * 3, 75 * 3), Image.LANCZOS)
head.save(os.path.join(out, "c12_head_left.png"))
head2 = im.crop((1200, 90, 2458, 165)).resize(((2458 - 1200) * 3, 75 * 3), Image.LANCZOS)
head2.save(os.path.join(out, "c12_head_right.png"))
print("saved", head.size, head2.size)
