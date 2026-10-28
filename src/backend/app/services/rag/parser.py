"""知识库文件解析：Excel/PDF/图片/TXT/MD/DOCX → 纯文本 + 结构化商品。

设计：
- 每个解析器返回 ParsedDoc：text（用于分块进向量库）与 products（Excel 商品行，
  进结构化表）。图片走视觉模型 image2text（未配置 key 时报可读错误，不静默降级）。
- Excel 表头采用宽松映射（_HEADER_ALIASES），兼容"商品名称/名称/name"等写法。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from loguru import logger

# 支持的商品表头别名 → Product 字段
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    # 别名顺序即优先级（先命中的列优先），兼容港式/繁体表头与英文字段名
    "sku": ("sku", "itemnum", "货号", "商品编号", "商品编码", "编码", "条码", "条形码"),
    "name": ("name", "商品名称", "产品名称", "品名", "貨名", "货名", "名称", "商品"),
    "price": ("price", "价格", "售价", "单价", "零售价", "出厂价", "itempx"),
    # 不收"数量"：报价单里的数量列是订购数量，误映射会让 Agent 对着 0 回答"已售罄"
    "stock": ("stock", "库存", "现货", "存货", "可售"),
    "category": ("category", "分类", "类目"),
    "tags": ("tags", "标签", "关键词"),
}
_SPEC_KEYS = ("规格", "参数", "specs", "型号", "尺寸", "颜色")
# 表头定位：报价单首行常是标题/汇总行，最多向下探测几行
_HEADER_SCAN_ROWS = 6
# 空值占位符：Excel 公式产出的 #N/A 之类不进语料，否则每条商品都带一串噪声。
# 全部小写存储——调用处比对的是 val.strip().lower()，写 "#N/A" 永远匹配不上（曾因此漏进语料）。
_NULLISH = {"", "#n/a", "n/a", "na", "-", "--", "null", "none", "xxxx"}


@dataclass
class ProductRow:
    sku: str = ""
    name: str = ""
    price: float | None = None
    stock: int | None = None
    category: str = ""
    specs: str = ""
    tags: str = ""


@dataclass
class ParsedDoc:
    text: str
    products: list[ProductRow] = field(default_factory=list)


def detect_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls", ".csv"):
        return "excel"
    if ext == ".pdf":
        return "pdf"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return "image"
    if ext == ".docx":
        return "docx"
    return "text"


def parse(path: Path, file_type: str | None = None) -> ParsedDoc:
    file_type = file_type or detect_type(path)
    fn = {"excel": _parse_excel, "pdf": _parse_pdf, "image": _parse_image,
          "docx": _parse_docx, "text": _parse_text}[file_type]
    logger.info("解析知识库文件: {} ({})", path.name, file_type)
    return fn(path)


# ---------------------------------------------------------------------------

def _parse_text(path: Path) -> ParsedDoc:
    return ParsedDoc(text=path.read_text(encoding="utf-8", errors="replace"))


def _parse_pdf(path: Path) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        if t:
            parts.append(f"[第{i}页]\n{t}")
    return ParsedDoc(text="\n\n".join(parts))


def _parse_docx(path: Path) -> ParsedDoc:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    # 表格行转 " | " 拼接文本
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return ParsedDoc(text="\n".join(parts))


def _parse_image(path: Path) -> ParsedDoc:
    """图片 → 视觉模型生成描述（image2text）。无 key 时抛错由上层记为 failed。"""
    from app.core.models.manager import get_vision_model, model_id, resolve_name
    from app.services import usage

    model = get_vision_model()
    import base64

    from langchain_core.messages import HumanMessage
    b64 = base64.b64encode(path.read_bytes()).decode()
    prompt_text = "请提取图中全部商品信息与文字内容（名称/价格/规格/卖点），按 Markdown 输出。"
    msg = HumanMessage(content=[
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:{_mime(path)};base64,{b64}"}},
    ])
    # 图片字节不进计量（base64 串会估出天文数字的 token），只按提示词估输入
    with usage.track("vision", provider=resolve_name(), model=model_id("vision")) as st:
        st["inputs"] = [prompt_text]
        resp = model.invoke([msg])
        st["usage"] = getattr(resp, "usage_metadata", None) or {}
        st["outputs"] = [resp.text]
    return ParsedDoc(text=f"[图片识别: {path.name}]\n{resp.text}")


def _mime(path: Path) -> str:
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "bmp": "image/bmp"}.get(path.suffix.lower().lstrip("."), "image/png")


def _parse_excel(path: Path) -> ParsedDoc:
    """Excel/CSV：逐 sheet 读取。若表头可映射为商品表则同时产出结构化行，
    并把每一行渲染成自然语言文本进向量库（描述性检索与精确查询双通道共用一份数据）。"""
    tables = [("csv", _read_csv(path))] if path.suffix.lower() == ".csv" else _read_xlsx(path)

    text_parts, products = [], []
    for sheet_name, rows in tables:
        hdr_i, mapping = _locate_header(rows)
        if hdr_i is None:
            hdr_i, mapping = 0, None
        header = [_cell(c) for c in rows[hdr_i]] if hdr_i < len(rows) else []
        # 表头之前的标题行（如"XX 2026-2027 新品产品明细"）单独保留，它是有用的检索上下文
        for pre in rows[:hdr_i]:
            title = " ".join(_cell(c) for c in pre if _cell(c))
            if title:
                text_parts.append(f"[工作表: {sheet_name}] {title}")
        text_parts.append(f"[工作表: {sheet_name}] 表头: {header}")
        for raw in rows[hdr_i + 1:]:
            cells = [_cell(c) for c in raw]
            if not any(cells):
                continue
            extras = _extra_fields(header, cells, mapping)
            pr = _row_to_product(cells, mapping) if mapping else None
            if pr and pr.name:
                products.append(pr)
                text_parts.append(_product_line(pr, extras))
            else:
                # 识别不出商品列（或该行为空名）时保留原始行渲染，绝不丢内容
                text_parts.append(" | ".join(cells) + (f" ｜{extras}" if extras else ""))
    return ParsedDoc(text="\n".join(text_parts), products=products)


def _extra_fields(header: list[str], cells: list[str], mapping: dict[str, int] | None) -> str:
    """未被商品字段映射吃掉的列，拼成 "列名=值" 附加到商品行文本里。

    报价单里"产品配置/尺寸/箱规/货期时间"这类描述列不进结构化表，但它们是
    买家真正会问的内容（一包含几张、什么时候到货），必须留在向量语料里。
    """
    if not mapping:
        return ""
    used = set(mapping.values())
    parts = []
    for i, val in enumerate(cells):
        if i in used or val.strip().lower() in _NULLISH:
            continue
        # 无表头的列（库存表里常见的 G/H 数量列）明确标注，避免模型把猜测当字段名
        label = header[i] if i < len(header) and header[i] else f"第{i + 1}列(无表头)"
        parts.append(f"{label}={val}")
    return "；".join(parts)


def _cell(value) -> str:
    """单元格→单行字符串。做三件清洗：
    - 压平内嵌换行：Excel 多行单元格（条码/尺寸/组合装清单）不压平会被 chunker 的
      splitlines 拆成"假行"，一行商品的字段散落到不同分块；
    - 日期去掉零时间：openpyxl 给 datetime，`2026-09-10 00:00:00` 不如 `2026-09-10`；
    - 整数值去掉 .0：openpyxl 数字一律 float，`24.0` 读起来像型号不像数量。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if not (value.hour or value.minute or value.second):
            return value.strftime("%Y-%m-%d")
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _locate_header(rows: list[list]) -> tuple[int | None, dict[str, int] | None]:
    """在前几行里找能识别出"名称"列的那行当表头；找不到则整表按普通文本处理。"""
    for i in range(min(_HEADER_SCAN_ROWS, len(rows))):
        mapping = _map_header([_cell(c) for c in rows[i]])
        if mapping:
            return i, mapping
    return None, None


def _product_line(p: ProductRow, extras: str = "") -> str:
    seg = [f"商品「{p.name}」"]
    if p.sku:
        seg.append(f"SKU:{p.sku}")
    if p.price is not None:
        seg.append(f"价格:{p.price}元")
    if p.stock is not None:
        seg.append(f"库存:{p.stock}件")
    if p.category:
        seg.append(f"分类:{p.category}")
    if p.specs:
        seg.append(f"规格:{p.specs}")
    if extras:
        seg.append(extras)
    return "，".join(seg)


def _read_xlsx(path: Path) -> list[tuple[str, list[list[str]]]]:
    """返回 [(sheet 名, 全部行)]；表头定位交给 _locate_header。"""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    tables = []
    for ws in wb.worksheets:
        rows = [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        if rows:
            tables.append((ws.title, rows))
    wb.close()
    return tables


def _read_csv(path: Path) -> list[list[str]]:
    content = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = content.decode("utf-8", errors="replace")
    return [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text)) if any(_cell(c) for c in row)]


def _map_header(header: list[str]) -> dict[str, int] | None:
    """返回 Product 字段 → 列下标 的映射；识别不出名称列则视为普通表格（仅进向量库）。

    规格类列（尺寸/型号/装箱数…）以 "spec:<列名>" 记入映射，既进结构化 specs，
    也不会被 _extra_fields 重复拼接。
    """
    norm = [str(h).strip().lower() for h in header]
    mapping: dict[str, int] = {}
    for key, aliases in _HEADER_ALIASES.items():
        # 按别名优先级找列（而非按列顺序），避免"条码资料"这类列抢走更该做 SKU 的 Itemnum
        for alias in aliases:
            if key in mapping:
                break
            a = alias.lower()
            for i, h in enumerate(norm):
                if i in mapping.values():
                    continue
                if a == h or a in h:
                    mapping[key] = i
                    break
    if "name" not in mapping:
        return None
    for i, h in enumerate(norm):
        if i in mapping.values() or not h:
            continue
        for spec in _SPEC_KEYS:
            if spec.lower() == h or spec.lower() in h:
                mapping[f"spec:{header[i].strip()}"] = i
                break
    return mapping


def _row_to_product(raw: list, mapping: dict[str, int]) -> ProductRow:
    def get(field_name: str) -> str:
        i = mapping.get(field_name, -1)
        return str(raw[i]).strip() if 0 <= i < len(raw) and raw[i] is not None else ""

    specs = []
    for key, idx in mapping.items():
        if key.startswith("spec:"):
            v = str(raw[idx]).strip() if raw[idx] is not None else ""
            if v:
                specs.append(f"{key[5:]}={v}")
    return ProductRow(
        sku=get("sku"), name=get("name"),
        price=_to_float(get("price")), stock=_to_int(get("stock")),
        category=get("category"), tags=get("tags"), specs="; ".join(specs),
    )


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace("元", "").replace("¥", "").strip())
    except ValueError:
        return None


def _to_int(s: str) -> int | None:
    try:
        return int(float(s))
    except ValueError:
        return None
