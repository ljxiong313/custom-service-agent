"""评测脚本（P6）：检索/商品精确查询必跑；对话质量断言在模型可用时执行。

用法（后端已在 7861 运行，任意目录都能跑，路径按脚本位置定位）：
    py eval/run_eval.py            # 默认 tester/test123456 账号
演示语料（data/demo_products.xlsx、demo_policy.txt）缺失时会自动补上传并等入库完成：
下面所有检索/商品断言都打在这两个文件上，而冒烟脚本跑完会删掉它自己上传的副本。
断言标准：
    retrieval: 期望文件出现在 top-5；product: 名称精确命中；
    chat: 回答包含任一期望关键词（3 条用例自动建会话，转人工用例跑完即结单复位）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import yaml

BASE = "http://127.0.0.1:7861/api/v1"
CASES = Path(__file__).parent / "cases.yaml"
# 演示语料跟着后端数据目录走，按脚本位置定位——从仓库根还是 src/backend 跑都是同一份文件
DEMO_DIR = Path(__file__).resolve().parents[1] / "src" / "backend" / "data"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEMO_FILES = [("demo_products.xlsx", XLSX), ("demo_policy.txt", "text/plain")]

# Windows 控制台默认 GBK，模型回答里的 emoji 会让 print 直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

passed = failed = skipped = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def login(c: httpx.Client) -> None:
    c.post(f"{BASE}/auth/register",
           json={"username": "tester", "password": "test123456"})
    r = c.post(f"{BASE}/auth/login",
               json={"username": "tester", "password": "test123456"})
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"


def ensure_demo_corpus(c: httpx.Client) -> None:
    """演示语料缺失时自动补上传（冒烟脚本会删掉自己上传的副本，评测集不能跟着一起没）。

    检索/商品断言全部打在 demo_products.xlsx / demo_policy.txt 上，库里没有它们
    就是环境前置没满足，不该算成质量失败；上传走 admin 接口，非 admin 账号直接跳过。
    """
    have = {f["name"] for f in c.get(f"{BASE}/knowledge/files").json()}
    missing = [(DEMO_DIR / name, mime) for name, mime in DEMO_FILES if name not in have]
    if not missing:
        return
    ids = []
    for path, mime in missing:
        with path.open("rb") as f:
            r = c.post(f"{BASE}/knowledge/files",
                       files={"file": (path.name, f, mime)})
        if r.status_code == 403:
            print(f"  [warn] 当前账号非 admin，无法补上传演示语料：{r.json().get('detail')}")
            return
        r.raise_for_status()
        ids.append(r.json()["id"])
    deadline = time.time() + 120
    while time.time() < deadline:
        rows = [f for f in c.get(f"{BASE}/knowledge/files").json() if f["id"] in ids]
        if rows and all(f["status"] not in ("pending", "parsing") for f in rows):
            break
        time.sleep(2)
    print(f"  [info] 演示语料已补上传：{[p.name for p, _ in missing]}")


def run_retrieval(c: httpx.Client, cases: list[dict]) -> None:
    print("[retrieval] 检索召回（向量不可用时自动降级 BM25）")
    for case in cases:
        hits = c.post(f"{BASE}/knowledge/search",
                      json={"query": case["query"], "top_k": 5}).json()
        names = {h["file_name"] for h in hits}
        check(case["id"], case["expect_file"] in names, f"got {names}")


def run_product(c: httpx.Client, cases: list[dict]) -> None:
    print("[product] 结构化商品精确查询")
    for case in cases:
        page = c.get(f"{BASE}/knowledge/products",
                     params={"q": case["q"], "limit": 200}).json()
        names = {r["name"] for r in page["items"]}
        check(case["id"], case["expect_name"] in names,
              f"got {names}（total={page['total']}）")


def stream_answer(c: httpx.Client, conv_id: int, message: str) -> tuple[str | None, str]:
    """SSE 消费一轮对话；返回 (回答文本, 错误)。400=模型未配置。"""
    deltas: list[str] = []
    with c.stream("POST", f"{BASE}/chat/completions",
                  json={"conversation_id": conv_id, "message": message},
                  timeout=120) as resp:
        if resp.status_code == 400:
            return None, "model-not-configured"
        event = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "delta":
                deltas.append(json.loads(line[5:])["text"])
            elif line.startswith("data:") and event == "error":
                return None, json.loads(line[5:])["message"]
    return "".join(deltas), ""


def run_chat(c: httpx.Client, cases: list[dict]) -> None:
    global skipped
    print("[chat] 对话质量断言（需有效 api_key，否则 SKIP）")
    for case in cases:
        conv = c.post(f"{BASE}/conversations", json={"title": "评测会话"}).json()
        answer, err = stream_answer(c, conv["id"], case["message"])
        # 未配置 / 密钥无效：环境问题而非质量问题，计 SKIP
        if err == "model-not-configured" or any(
                m in err for m in ("Incorrect API key", "invalid_api_key", "未配置")):
            print(f"  SKIP  {case['id']}（模型 key 未配置或无效）")
            skipped += 1
            _cleanup_handoff(c, conv["id"])
            continue
        if err:
            check(case["id"], False, err)
            _cleanup_handoff(c, conv["id"])
            continue
        hit = any(k in (answer or "") for k in case["expect_any"])
        check(case["id"], hit, f"answer={answer!r:.80}")
        _cleanup_handoff(c, conv["id"])


def _cleanup_handoff(c: httpx.Client, conv_id: int) -> None:
    """转人工用例复位：结单恢复 bot；评测会话用完即删，保持数据整洁。"""
    for t in c.get(f"{BASE}/handoff/tickets", params={"status_filter": "open"}).json():
        if t["conversation_id"] == conv_id:
            c.post(f"{BASE}/handoff/tickets/{t['id']}/close")
    c.delete(f"{BASE}/conversations/{conv_id}")


def main() -> int:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    # httpx 默认超时只有 5 秒：检索首请求要现建 BM25 + jieba 词典加载 + rerank 外呼，
    # 冷启动必然超过，不显式放宽就会把环境差异误判成评测失败
    c = httpx.Client(timeout=90)
    login(c)
    ensure_demo_corpus(c)
    run_retrieval(c, cases.get("retrieval", []))
    run_product(c, cases.get("product", []))
    run_chat(c, cases.get("chat", []))
    print(f"\n===== {passed} passed, {failed} failed, {skipped} skipped =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
