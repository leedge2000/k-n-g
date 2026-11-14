"""中国统计年鉴图像表格采集与解析。

国家统计局《中国统计年鉴》自改版后将统计表以 JPG 图片发布（如
``sj/ndsj/2025/html/C12-10.jpg``），HTML 中不含可解析的表格结构。
本模块把「图片统计表」接成一等采集能力：

1. ``parse_catalog``  解析年鉴左侧目录页（GBK），得到表号、表名与图片地址；
2. ``prepare_images`` 裁剪白边并放大、分块，提升 OCR 命中率；
3. ``ocr_image``      调用 Windows 内置中文 OCR（无外部服务依赖）；
4. ``group_rows`` / ``parse_table`` 依据文字坐标还原行列；
5. ``parse_value``    恢复被 OCR 拆散的小数位，拒绝含非数字字符的可疑单元格。

解析产物写入 ``data/yearbook_parsed/<图片sha256>.json`` 缓存，抽取器优先读缓存，
便于对某张表做人工或视觉校核后直接复用，而无需重复 OCR。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image

OCR_SCRIPT = Path(__file__).with_name("yearbook_ocr.ps1")

# 表格行聚类容差（OCR 放大坐标）
ROW_YTOL = 14
OCR_SCALE = 3

# 年鉴表列宽固定，列中心等距排布：原图 x = 136 + 85.35·k，年份列位于 k=-1。
# 不同年代只是在部分列留空（如 1995 年前的「谷物」），因此按坐标取整定位列，
# 而不是按出现顺序映射，避免空列导致整体错位。常数按 OCR 放大坐标给出。
GRID_X0 = 136.0 * OCR_SCALE
GRID_STEP = 85.35 * OCR_SCALE

# 12-10 主要农产品产量的列序（国家统计局年鉴表头顺序，2025 版）
COLUMNS: tuple[str, ...] = (
    "粮食", "谷物", "稻谷", "小麦", "玉米", "豆类", "大豆", "薯类",
    "棉花", "油料", "花生", "油菜籽", "芝麻", "麻类", "糖料", "甘蔗",
    "甜菜", "烟叶", "烤烟", "蚕茧", "桑蚕茧", "茶叶", "水果",
    "苹果", "柑橘", "梨", "葡萄", "香蕉",
)

# 导入列的合理量级区间（万吨），用于拦截 OCR 位数错乱；超界即判为识别失败
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "粮食": (20000.0, 80000.0),
    "稻谷": (8000.0, 25000.0),
    "小麦": (3000.0, 16000.0),
    "玉米": (3000.0, 35000.0),
    "大豆": (500.0, 3000.0),
}

# 表号 -> 解析规格；未列入的表不导入，避免用错列名
TABLE_SPECS: dict[str, dict[str, Any]] = {
    "12-10": {
        "title": "主要农产品产量",
        "unit": "万吨",
        "columns": COLUMNS,
        "categoryId": "KTC0201",
    },
}


@dataclass
class CatalogEntry:
    table_no: str
    title: str
    image_url: str


@dataclass
class ParsedTable:
    table_no: str
    title: str
    unit: str
    image_sha256: str
    image_url: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def parse_catalog(html: str) -> list[CatalogEntry]:
    """从年鉴目录页解析表号 -> 表名 -> 图片地址。

    目录页使用 ``12-10 主要农产品产量`` 与 ``html/C12-10.jpg`` 的命名约定，
    表号 ``12-10`` 对应文件名 ``C12-10.jpg``。
    """
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for raw in text.split("\n"):
        line = raw.strip().replace("\u3000", " ")
        match = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})\s+(.{2,60})$", line)
        if not match:
            continue
        chapter, index = int(match.group(1)), int(match.group(2))
        table_no = f"{chapter}-{index}"
        if table_no in seen:
            continue
        title = match.group(3).strip()
        if not title or title.startswith("#"):
            continue
        seen.add(table_no)
        entries.append(CatalogEntry(
            table_no=table_no,
            title=title,
            image_url=f"html/C{chapter:02d}-{index:02d}.jpg",
        ))
    return entries


def guess_image_url(catalog_html: str, table_no: str) -> str | None:
    """按表号在目录页中定位图片地址。"""
    chapter, index = table_no.split("-")
    pattern = rf"(?:\./)?html/C{int(chapter):02d}-{int(index):02d}\.jpg"
    match = re.search(pattern, catalog_html, flags=re.I)
    if match:
        return match.group(0).lstrip("./")
    return f"html/C{int(chapter):02d}-{int(index):02d}.jpg"


def prepare_images(source: Path, workdir: Path) -> list[Path]:
    """裁剪白边、放大 3 倍并按上下切块，提升中文印刷体 OCR 命中率。"""
    workdir.mkdir(parents=True, exist_ok=True)
    image = Image.open(source).convert("RGB")
    bbox = image.convert("L").point(lambda v: 0 if v > 245 else 255).getbbox()
    if bbox:
        image = image.crop(bbox)
    scale = OCR_SCALE
    width, height = image.size
    parts: list[Path] = []
    # 单块过高时 OCR 易丢字符，按目标高度均分
    slices = max(1, round(height * scale / 3400))
    step = height // slices
    for i in range(slices):
        top = i * step
        bottom = height if i == slices - 1 else (i + 1) * step
        tile = image.crop((0, top, width, bottom))
        tile = tile.resize((width * scale, (bottom - top) * scale), Image.LANCZOS)
        path = workdir / f"part{i}.png"
        tile.save(path)
        parts.append(path)
    return parts


def ocr_image(image_path: Path, powershell: str = "powershell") -> dict[str, Any]:
    """调用 Windows 内置 OCR，返回含词级坐标的识别结果。"""
    completed = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OCR_SCRIPT),
         "-ImagePath", str(Path(image_path).resolve())],  # WinRT 要求绝对路径
        capture_output=True, timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"OCR 失败：{completed.stderr.decode('utf-8', 'ignore')[:300]}")
    stdout = completed.stdout.decode("utf-8-sig", "ignore").strip()
    if not stdout:
        raise RuntimeError("OCR 未返回结果")
    return json.loads(stdout)


def capture_image(url: str, out_path: Path) -> Path:
    """用无头浏览器把页面渲染结果截图为 PNG。

    作为直链下载的备用通道：部分站点的图片/表格资源带防盗链或只在浏览器中
    渲染出内容。浏览器按 ``device_scale_factor=1`` 出图，像素与原始资源一致，
    放大仍交给 ``prepare_images`` 统一处理，避免二次缩放拉低识别率。
    """
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            # 大视口加载，图片按 1:1 原始像素呈现；再缩到图片尺寸整屏截图，
            # 避免 clip / 元素截图触发重采样，导致 OCR 坐标与网格错位
            context = browser.new_context(viewport={"width": 4096, "height": 4096},
                                          device_scale_factor=1)
            page = context.new_page()
            page.goto(url, wait_until="load", timeout=60000)
            page.wait_for_timeout(300)
            size = page.evaluate(
                "() => {const i = document.querySelector('img');"
                " return i ? {w: i.naturalWidth, h: i.naturalHeight} : null;}"
            )
            if size and size["w"] and size["h"]:
                page.set_viewport_size({"width": size["w"], "height": size["h"]})
                page.wait_for_timeout(400)
            page.screenshot(path=str(out_path), type="png")
            return out_path
        finally:
            browser.close()


def group_rows(payload: dict[str, Any], y_tol: int = ROW_YTOL) -> list[list[dict[str, Any]]]:
    """按文字纵向中心聚类成行，行内按横坐标排序。"""
    words: list[dict[str, Any]] = []
    for line in payload.get("lines", []):
        words.extend(line.get("words", []))
    words.sort(key=lambda w: w["y"] + w["h"] / 2)
    rows: list[dict[str, Any]] = []
    for word in words:
        center = word["y"] + word["h"] / 2
        if rows and abs(center - rows[-1]["cy"]) <= y_tol:
            rows[-1]["words"].append(word)
            rows[-1]["cy"] = (rows[-1]["cy"] + center) / 2
        else:
            rows.append({"cy": center, "words": [word]})
    for row in rows:
        row["words"].sort(key=lambda w: w["x"])
    return [row["words"] for row in rows]


_DIGITS = str.maketrans({"０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
                         "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
                         "Ｏ": "0", "O": "0", "o": "0", "．": ".", "·": ".", "·": "."})


def parse_value(tokens: list[str]) -> float | None:
    """把 OCR 词序列还原为数值；无法确定时返回 None，绝不猜补。

    年鉴表数值统一保留一位小数，OCR 常把 ``30476.5`` 拆成 ``30476`` ``5``，
    因此对「末尾单字符数字」追加小数位；出现非数字字符（如 ``&``）一律放弃。
    """
    if not tokens:
        return None
    joined = "".join(tokens).translate(_DIGITS).replace(" ", "")
    if not joined:
        return None
    if "." in joined:
        if re.fullmatch(r"\d+\.\d+", joined):
            return float(joined)
        return None
    if not joined.isdigit():
        return None
    if len(tokens) > 1 and len(tokens[-1]) == 1 and len(joined) >= 3:
        return float(f"{joined[:-1]}.{joined[-1]}")
    return float(joined)


_YEAR = re.compile(r"^(19|20)\d{2}$")


def parse_table(table_no: str, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """从单块 OCR 结果解析数据行，返回（行列表, 被拒绝的原始行）。

    列定位按等距网格取整：先按词中心坐标归入列序号（年份为 -1），
    再对同列词按横坐标拼接解析，避免空列或跨词切分造成错位。
    """
    spec = TABLE_SPECS.get(table_no)
    if not spec:
        return [], []
    columns = spec["columns"]
    rows: list[dict[str, Any]] = []
    rejected: list[str] = []
    for words in group_rows(payload):
        buckets: dict[int, list[dict[str, Any]]] = {}
        for word in words:
            center = word["x"] + word["w"] / 2
            index = round((center - GRID_X0) / GRID_STEP)
            buckets.setdefault(index, []).append(word)
        year_text = "".join(w["text"] for w in buckets.get(-1, [])).strip()
        if not _YEAR.fullmatch(year_text):
            # 年份被 OCR 截断（如 2014 只识别出「20」）时如实记录，不做推断
            if re.fullmatch(r"20\d{0,2}", year_text):
                rejected.append(f"{year_text}: 年份识别不完整")
            continue
        values: dict[str, float] = {}
        for index, items in sorted(buckets.items()):
            if index < 0 or index >= len(columns):
                continue
            items.sort(key=lambda w: w["x"])
            value = parse_value([w["text"] for w in items])
            if value is None:
                continue
            name = columns[index]
            bounds = PLAUSIBLE.get(name)
            if bounds and not (bounds[0] <= value <= bounds[1]):
                rejected.append(f"{year_text}: {name}={value:g} 超出合理区间")
                continue
            values[name] = value
        if "粮食" not in values:
            rejected.append(f"{year_text}: 粮食列未识别")
            continue
        rows.append({"year": int(year_text), "values": values})
    return rows, rejected


def validate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """跨年份一致性校验，剔除明显误识别的行。"""
    ordered = sorted(rows, key=lambda r: r["year"])
    kept: list[dict[str, Any]] = []
    notes: list[str] = []
    previous_food: float | None = None
    for row in ordered:
        food = row["values"].get("粮食")
        if food is None or not (20000 <= food <= 80000):
            notes.append(f"{row['year']}: 粮食产量 {food} 超出合理区间")
            continue
        if previous_food is not None:
            change = abs(food - previous_food) / previous_food
            if change > 0.25:
                notes.append(f"{row['year']}: 粮食环比变动 {change:.1%} 异常")
                continue
        sugar, cane, beet = (row["values"].get(k) for k in ("糖料", "甘蔗", "甜菜"))
        if None not in (sugar, cane, beet) and sugar > 0:
            drift = abs((cane + beet) - sugar) / sugar
            if drift > 0.02:
                notes.append(f"{row['year']}: 糖料与甘蔗+甜菜偏差 {drift:.1%}")
        kept.append(row)
        previous_food = food
    return kept, notes


def parse_image(image_path: Path, table_no: str, cache_dir: Path) -> ParsedTable:
    """完整解析一张年鉴表图片：OCR（带缓存）→ 行列还原 → 跨年校验。"""
    spec = TABLE_SPECS[table_no]
    image_sha = sha256_file(image_path)
    cached = load_cache(cache_dir, image_sha)
    if cached is None:
        with tempfile.TemporaryDirectory() as tmp:
            parts = prepare_images(image_path, Path(tmp))
            payloads = [ocr_image(part) for part in parts]
        cached = {"parts": payloads}
        save_cache(cache_dir, image_sha, cached)
    rows: list[dict[str, Any]] = []
    rejected: list[str] = []
    seen: set[int] = set()
    for payload in cached.get("parts", []):
        part_rows, part_rejected = parse_table(table_no, payload)
        rejected.extend(part_rejected)
        for row in part_rows:
            if row["year"] in seen:
                continue
            seen.add(row["year"])
            rows.append(row)
    kept, notes = validate_rows(rows)
    return ParsedTable(
        table_no=table_no,
        title=spec["title"],
        unit=spec["unit"],
        image_sha256=image_sha,
        image_url="",
        columns=list(spec["columns"]),
        rows=kept,
        rejected=rejected + notes,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_cache(cache_dir: Path, image_sha: str) -> dict[str, Any] | None:
    path = cache_dir / f"{image_sha}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache(cache_dir: Path, image_sha: str, payload: dict[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{image_sha}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
