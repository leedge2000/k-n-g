"""年鉴图像表格解析的纯函数测试（不联网、不调用 OCR）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import yearbook


def word(text: str, center_x: float, center_y: float = 100.0, width: float = 60.0) -> dict:
    return {"text": text, "x": center_x - width / 2, "y": center_y - 30.0,
            "w": width, "h": 60.0}


def payload(rows: list[list[dict]]) -> dict:
    return {"lines": [{"text": "", "words": row} for row in rows]}


def column_x(index: int) -> float:
    """列序号（-1 为年份列）对应的文字中心坐标。"""
    return yearbook.GRID_X0 + yearbook.GRID_STEP * index


def test_parse_catalog_reads_table_no_and_image_url():
    html = """<html><script>var a = "1-1 干扰项";</script>
    <a href="html/C12-10.jpg">12-10 主要农产品产量</a>
    <a href="html/C12-10.jpg">12-10 主要农产品产量</a>
    <a href="html/C12-11.jpg">12-11 主要农产品单位面积产量</a>
    </html>"""
    entries = yearbook.parse_catalog(html)
    assert [entry.table_no for entry in entries] == ["12-10", "12-11"]
    assert entries[0].title == "主要农产品产量"
    assert entries[0].image_url == "html/C12-10.jpg"


def test_parse_value_restores_split_decimal_and_rejects_unknown_chars():
    assert yearbook.parse_value(["30476", "5"]) == 30476.5
    assert yearbook.parse_value(["13693．0"]) == 13693.0
    assert yearbook.parse_value(["33", "．", "8"]) == 33.8
    # 识别结果含非数字字符时放弃，不做猜测
    assert yearbook.parse_value(["13", "&", "1"]) is None
    assert yearbook.parse_value([]) is None


def test_parse_table_maps_columns_by_x_position_without_empty_column_shift():
    # 1978 年无「谷物」列，但稻谷/小麦/玉米/大豆仍按坐标归位
    row = [
        word("1978", column_x(-1)),
        word("30476", column_x(0)), word("5", column_x(0) + 70),
        word("13693", column_x(2)), word("0", column_x(2) + 70),
        word("5384", column_x(3)), word("0", column_x(3) + 70),
        word("5594", column_x(4)), word("5", column_x(4) + 70),
        word("756", column_x(6)), word("5", column_x(6) + 70),
    ]
    rows, rejected = yearbook.parse_table("12-10", payload([row]))
    assert rejected == []
    assert len(rows) == 1
    values = rows[0]["values"]
    assert rows[0]["year"] == 1978
    assert values["粮食"] == 30476.5
    assert values["稻谷"] == 13693.0
    assert values["小麦"] == 5384.0
    assert values["玉米"] == 5594.5
    assert values["大豆"] == 756.5
    assert "谷物" not in values


def test_parse_table_accepts_modern_layout_with_grain_subtotal():
    row = [
        word("1995", column_x(-1)),
        word("46661", column_x(0)), word("8", column_x(0) + 70),
        word("41611", column_x(1)), word("6", column_x(1) + 70),
        word("18522", column_x(2)), word("6", column_x(2) + 70),
        word("10220", column_x(3)), word("7", column_x(3) + 70),
        word("11198", column_x(4)), word("6", column_x(4) + 70),
        word("1350", column_x(6)), word("2", column_x(6) + 70),
    ]
    rows, rejected = yearbook.parse_table("12-10", payload([row]))
    assert rejected == []
    values = rows[0]["values"]
    assert values["粮食"] == 46661.8
    assert values["谷物"] == 41611.6
    assert values["稻谷"] == 18522.6
    assert values["小麦"] == 10220.7
    assert values["大豆"] == 1350.2


def test_parse_table_rejects_implausible_grain_value():
    row = [
        word("2023", column_x(-1)),
        word("5", column_x(0)),
        word("20660", column_x(2)), word("3", column_x(2) + 70),
    ]
    rows, rejected = yearbook.parse_table("12-10", payload([row]))
    assert rows == []
    assert any("粮食" in item for item in rejected)


def test_parse_table_ignores_rows_without_year():
    row = [word("河北", column_x(-1)), word("3865", column_x(0))]
    rows, rejected = yearbook.parse_table("12-10", payload([row]))
    assert rows == []
    assert rejected == []
