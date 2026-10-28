"""演示语料生成器：data/demo_products.xlsx 与 demo_policy.txt 缺失时自动补出来。

为什么需要它：这两个文件在 `.gitignore` 的 `src/backend/data/` 屏蔽范围内（data/ 整个
目录是运行时产物 + 明文密钥的地带），所以**没有入库**。结果是全新检出（CI、新同事、
容器里）跑 `smoke_test.py` / `platform_smoke.py` / `eval/run_eval.py` 会在"上传演示文件"
这一步 FileNotFoundError——而评测的 9 条断言全都打在这两份语料上。

语料内容按 `eval/cases.yaml` 的断言倒推（SKU001=129 元、精华命中烟酰胺精华原液、
政策里有 7 天无理由/满 199 包邮/会员 9 折），改了这边的数据就要改那边的期望。
"""
from __future__ import annotations

from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[1] / "data"

PRODUCT_HEADER = ("商品名称", "SKU", "价格", "库存", "分类", "标签", "规格")
PRODUCT_ROWS = [
    ("清爽保湿面霜50g", "SKU001", 129, 35, "护肤", "保湿;国货", "50g/瓶装"),
    ("氨基酸洁面乳", "SKU002", 59.9, 120, "清洁", "温和;敏感肌", "100ml"),
    ("烟酰胺精华原液", "SKU003", 199, 8, "精华", "提亮", "30ml"),
    ("胶原蛋白饮", "SKU004", 358, 0, "口服美容", "礼盒", "10瓶/箱"),
]

POLICY_TEXT = """售后政策
1. 7天无理由退换（未拆封）。
2. 满199元包邮，会员9折。
3. 客服工作时间 9:00-21:00。
"""


def ensure_demo_files(demo_dir: Path = DEMO_DIR) -> list[Path]:
    """补齐缺失的演示文件，返回本次真正写出来的路径（已存在则返回空列表）。

    xlsx 用 openpyxl 现场生成：它是运行依赖（Excel 知识库解析本来就要用），
    不为此新增任何依赖，也不往 git 里塞二进制。
    """
    demo_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    xlsx = demo_dir / "demo_products.xlsx"
    if not xlsx.exists():
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "products"
        ws.append(list(PRODUCT_HEADER))
        for row in PRODUCT_ROWS:
            ws.append(list(row))
        wb.save(xlsx)
        written.append(xlsx)
    policy = demo_dir / "demo_policy.txt"
    if not policy.exists():
        policy.write_text(POLICY_TEXT, encoding="utf-8")
        written.append(policy)
    return written


if __name__ == "__main__":
    made = ensure_demo_files()
    print("已生成：" + ", ".join(m.name for m in made) if made else "演示语料已齐，无需生成")
