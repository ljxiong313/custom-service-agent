"""RAG 数据质量单测：Excel 清洗、表头定位、分块与 RRF 融合。

这些断言对应历史上真出过的错（见 docs/代码规范.md 与记忆里的"Excel 解析三坑"），
不调模型、不建向量，纯函数级，跑在临时配置上。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.rag.chunker import chunk_text
from app.services.rag.fusion import rrf_fusion
from app.services.rag.parser import (
    ParsedDoc,
    ProductRow,
    _cell,
    _extra_fields,
    _locate_header,
    _map_header,
    _parse_excel,
    _row_to_product,
    detect_type,
)

# ---- 单元格清洗 ----

@pytest.mark.parametrize("value,expected", [
    (24.0, "24"),                    # openpyxl 数字一律 float，别让"24.0"进语料
    (24.5, "24.5"),
    (None, ""),
    (datetime(2026, 9, 10), "2026-09-10"),           # 零时间日期去掉 00:00:00
    (datetime(2026, 9, 10, 8, 30), "2026-09-10 08:30"),
    (date(2026, 9, 10), "2026-09-10"),
    ("条码A\n条码B", "条码A 条码B"),   # 内嵌换行必须压平，否则 chunker 拆成假行
    ("  多  空格  ", "多 空格"),
])
def test_cell_flattens_and_normalizes(value, expected):
    assert _cell(value) == expected


def _rows_to_doc(tmp_path, rows: list[list], name: str = "rows.xlsx") -> ParsedDoc:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "库存"
    for r in rows:
        ws.append(r)
    path = tmp_path / name
    wb.save(path)
    return _parse_excel(path)


def test_multiline_cell_stays_one_row(tmp_path):
    """压平后整行只剩一条换行边界，一行商品不会被切进两个分块。"""
    doc = _rows_to_doc(tmp_path, [["名称", "备注"], ["面霜", "含 30ml×2\n赠品一片"]])
    assert doc.products[0].name == "面霜"
    assert any("赠品一片" in c for c in chunk_text(doc.text))


# ---- 表头识别 ----

def test_map_header_alias_priority():
    # Itemnum 比"条码资料"更该做 SKU：别名顺序即优先级
    m = _map_header(["条码资料", "Itemnum", "商品名称", "售价", "库存"])
    assert m["sku"] == 1 and m["name"] == 2 and m["price"] == 3 and m["stock"] == 4


def test_map_header_requires_name_column():
    assert _map_header(["货号", "售价", "库存"]) is None


def test_map_header_collects_spec_columns():
    m = _map_header(["商品名称", "尺寸", "箱规"])
    assert m["name"] == 0
    assert "spec:尺寸" in m                  # 尺寸是登记的规格类别名
    assert "spec:箱规" not in m              # 未登记的列不猜字段名，交给 _extra_fields 原文保留


def test_locate_header_skips_title_rows():
    rows = [["XX 球星卡 2026-2027 新品报价单"], ["合计"], ["货号", "商品名称", "价格"],
            [["A1", "卡册", 39.0]][0]]
    i, mapping = _locate_header(rows)
    assert i == 2 and mapping and mapping["name"] == 1


# ---- 结构化行与附加列 ----

def test_row_to_product_number_formatting():
    m = _map_header(["货号", "商品名称", "价格", "库存"])
    p = _row_to_product(["SKU1", "面霜", "39.0", "12.0"], m)
    assert isinstance(p, ProductRow) and p.sku == "SKU1" and p.price == 39.0 and p.stock == 12


def test_extra_fields_keeps_unmapped_and_unlabeled():
    header = ["商品名称", "价格", "货期时间", ""]
    cells = ["面霜", "39", "7 天", "#N/A"]
    m = _map_header(header)
    extras = _extra_fields(header, cells, m)
    assert "货期时间=7 天" in extras            # 买家真会问的列不能丢
    assert "#N/A" not in extras                 # 空值占位符不进语料（大小写也要拦住）
    assert "第4列" not in extras
    # 有值但没表头的列要显式标注"无表头"，别让模型把猜测当字段名
    assert "第4列(无表头)=可发" in _extra_fields(header, ["面霜", "39", "7 天", "可发"], m)


def test_parse_excel_end_to_end(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["2026 秋季新品报价单"])          # 标题行：不是表头，但要留在语料里
    ws.append(["货号", "商品名称", "售价", "库存", "产品配置", ""])
    ws.append(["A001", "保湿面霜 50g", 129.0, 36.0, "1 瓶 2 支", "#N/A"])
    ws.append(["A002", "防晒霜 30ml", 89.5, 0.0, "SPF50+\n防水", None])
    path = tmp_path / "报价.xlsx"
    wb.save(path)

    doc = _parse_excel(path)
    assert [p.name for p in doc.products] == ["保湿面霜 50g", "防晒霜 30ml"]
    assert doc.products[0].price == 129.0 and doc.products[0].stock == 36
    assert doc.products[1].stock == 0                      # 0 是有效库存，不是空值
    assert "产品配置=1 瓶 2 支" in doc.text
    assert "2026 秋季新品报价单" in doc.text               # 标题行保留为检索上下文
    assert "防水" in doc.text and "SPF50+" in doc.text      # 多行单元格压平后仍在同一行
    assert "#N/A" not in doc.text
    assert len(doc.text.splitlines()) == 1 + 1 + 2         # 标题 + 表头 + 两条商品行


def test_detect_type():
    from pathlib import Path

    assert detect_type(Path("a.XLSX")) == "excel"
    assert detect_type(Path("a.csv")) == "excel"
    assert detect_type(Path("a.pdf")) == "pdf"
    assert detect_type(Path("a.JPG")) == "image"
    assert detect_type(Path("a.docx")) == "docx"
    assert detect_type(Path("a.md")) == "text"


# ---- 分块 ----

def test_chunk_keeps_one_product_line_whole():
    lines = [f"商品「条目{i}」，价格:{i}元" for i in range(40)]
    chunks = chunk_text("\n".join(lines))
    assert len(chunks) > 1                        # 超出 200 token 预算必然分块
    for c in chunks:
        for line in c.splitlines():
            assert line.startswith("商品「")       # 没有半行商品出现


def test_chunk_overlap_carries_tail_lines():
    lines = [f"第{i}行 商品描述" for i in range(60)]
    chunks = chunk_text("\n".join(lines))
    assert chunks[0].splitlines()[-1] in chunks[1].splitlines()


def test_chunk_hard_splits_overlong_line():
    chunks = chunk_text("一" * 900)               # 单行远超 chunk_size
    assert len(chunks) >= 2
    assert "".join(c.replace("\n", "") for c in chunks).count("一") == 900


def test_chunk_ignores_blank_lines():
    assert chunk_text("\n\n   \n") == []


# ---- RRF 融合 ----

def test_rrf_rewards_both_lists():
    vec = [(1, 0.9, 10), (2, 0.8, 10)]
    bm = [(2, 5.0, 10), (3, 4.0, 10)]
    hits = rrf_fusion(vec, bm, k=60)
    assert hits[0].chunk_id == 2                  # 两路都命中的排最前
    assert [h.chunk_id for h in hits] == [2, 1, 3]
    assert hits[0].score > hits[1].score > hits[2].score


def test_rrf_score_is_rank_only():
    # 分数量纲不同（cosine 0.9 vs bm25 5.0）不影响名次，只看排名
    a = rrf_fusion([(7, 0.99, 1)], [(7, 500.0, 1)], k=60)
    b = rrf_fusion([(7, 0.11, 1)], [(7, 0.2, 1)], k=60)
    assert abs(a[0].score - b[0].score) < 1e-9
