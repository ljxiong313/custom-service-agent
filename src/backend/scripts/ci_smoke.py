"""CI 冒烟编排：自动拉起一个隔离的后端实例，跑完三套端到端脚本再收掉。

为什么需要它：smoke_test.py（45 项）、platform_smoke.py（29 项）、eval/run_eval.py（9 项）
是项目里**唯一真打模型与真走 HTTP 的验证**，但它们都要求"后端已经在 7861 上跑着"，
因此进不了 CI——离线单测全绿而端到端坏掉，正是这个系统最容易出的事故形态
（SSE 协议、回调路由、静态文件鉴权这些只在真 HTTP 栈上才暴露）。

做法与取舍：
- 用 uvicorn 子进程而不是 TestClient：SSE 流式、后台 sweeper、 lifespan 起停、
  真实端口上的回调幂等，这些在进程内测试客户端上要么行为不同要么根本测不到。
- 独立临时 CS_CONFIG（临时 SQLite + 临时 Chroma + 随机 jwt_secret + 大限额）：
  绝不碰开发者本机 data/，同时顺带验证"空库自举出第一个 admin"这条首次部署路径。
- 模型 key 留空：检索/商品/平台链路都能在降级路径上跑通；对话质量断言自动 SKIP。
  要跑真模型的完整评测，把 CS_CONFIG 换成 config.local.yaml 后单独执行各脚本。

用法（仓库根）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/../src/backend/scripts/ci_smoke.py
    或 cd src/backend && py scripts/ci_smoke.py [--keep-running]
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parents[1]
TEMPLATES = [BACKEND / "config.simple.yaml"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_ci_config(tmp: Path, fixed_port: int | None = None) -> tuple[Path, int]:
    """从仓库默认配置派生一份 CI 配置：路径全指到临时目录，限额放宽。"""
    base: dict = {}
    for t in TEMPLATES:
        if t.exists():
            base = yaml.safe_load(t.read_text(encoding="utf-8")) or {}
            break
    if not base:
        raise SystemExit("找不到 config.simple.yaml，无法派生 CI 配置")

    base.setdefault("server", {}).update({
        "host": "127.0.0.1", "port": fixed_port or _free_port(), "env": "dev"})
    base.setdefault("database", {})["url"] = f"sqlite:///{(tmp / 'ci.db').as_posix()}"
    sec = base.setdefault("security", {})
    sec["jwt_secret"] = secrets.token_hex(32)          # 够长，避开 startup_check 的短密钥告警
    sec["metrics_token"] = "ci-metrics-token"
    sec["rate_limit"] = {"enabled": True,              # 限流保留开启（要测），但限额放到很大
                         "default": {"per_minute": 100000, "burst": 100000},
                         "rules": []}
    llm = base.setdefault("llm", {})
    llm.update({"request_timeout_seconds": 5, "max_retries": 0})
    base.setdefault("rag", {}).update({"chroma_path": str(tmp / "chroma"), "ingest_workers": 2})
    base.setdefault("session", {}).update({"idle_seconds": 5, "close_after_seconds": 3,
                                          "sweep_interval_seconds": 2})
    base.setdefault("wecom", {})["mock"] = True
    platforms = base.setdefault("platforms", {})
    for conf in platforms.values():
        if isinstance(conf, dict):
            conf.update({"enabled": True, "mock": True, "callback_verify": "sign"})
    for name, conf in (base.get("multi_models") or {}).items():
        if isinstance(conf, dict):
            conf["api_key"] = os.environ.get(f"CS_CI_API_KEY_{name.upper()}", "")

    out = tmp / "config.ci.yaml"
    out.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out, int(base["server"]["port"])


def wait_ready(base: str, timeout: float = 60.0) -> dict:
    """轮询 /ready 而不是 /health：就绪探针会确认库可查、迁移已跑。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/ready", timeout=5)
            last = r.json()
            if r.status_code == 200 and last.get("ready"):
                return last
        except Exception:
            pass
        time.sleep(0.5)
    return last


def _wait_forever() -> None:
    """挂住进程直到被 Ctrl+C / 终止：交互态等回车，被 CI 或后台拉起时 stdin 可能是
    一个立刻 EOF 的伪终端，靠 input() 判断会直接崩，所以 EOF 后转入定时休眠。"""
    try:
        input()
    except (EOFError, RuntimeError):
        while True:
            time.sleep(3600)


def run_suite(name: str, argv: list[str], env: dict[str, str], cwd: Path) -> tuple[str, int, float]:
    started = time.monotonic()
    print(f"\n===== {name} =====\n$ {' '.join(argv)}")
    proc = subprocess.run(argv, cwd=str(cwd), env=env)
    elapsed = time.monotonic() - started
    status = "PASS" if proc.returncode == 0 else f"FAIL(rc={proc.returncode})"
    print(f"[{name}] {status}  用时 {elapsed:.1f}s")
    return status, proc.returncode, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端冒烟编排（自动起服务、隔离临时数据）")
    parser.add_argument("--keep-running", action="store_true",
                        help="跑完不关服务，打印地址便于手工验证")
    parser.add_argument("--suites", default="smoke,platform,eval",
                        help="逗号分隔：smoke / platform / eval；none 表示只起服务")
    parser.add_argument("--port", type=int, default=0,
                        help="固定端口（默认挑一个空闲口）。要对着 vite 的 7861 代理验证时指定")
    parser.add_argument("--serve-only", action="store_true",
                        help="只起隔离服务不跑套件（手工在浏览器/接口上验证用）")
    args = parser.parse_args()
    if args.serve_only:
        args.suites = "none"

    python = sys.executable
    tmp = Path(tempfile.mkdtemp(prefix="cs-agent-ci-"))
    config, port = build_ci_config(tmp, fixed_port=args.port or None)
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CS_CONFIG": str(config), "CS_BASE_URL": base,
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    # 演示语料不在 git 里（data/ 被 gitignore），全新检出必须先补出来，
    # 否则 smoke/eval 会在上传那步 FileNotFoundError
    try:
        import demo_corpus  # type: ignore[import-not-found]

        made = demo_corpus.ensure_demo_files()
        if made:
            print(f"演示语料已现场生成：{', '.join(m.name for m in made)}")
    except Exception as e:
        print(f"[warn] 演示语料预生成失败（套件可能因缺文件报错）: {e}")

    print(f"CI 服务: {base}  配置: {config}  数据: {tmp}")
    server = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND), env=env)
    try:
        ready = wait_ready(base)
        if not ready.get("ready"):
            print(f"服务未就绪，/ready 返回: {ready}")
            return 1
        print(f"就绪: {ready['checks']}")

        selected = {s.strip() for s in args.suites.split(",") if s.strip()}
        results = []
        if "smoke" in selected:
            results.append(("smoke_test", run_suite(
                "smoke_test", [python, "scripts/smoke_test.py"], env, BACKEND)))
        if "platform" in selected:
            results.append(("platform_smoke", run_suite(
                "platform_smoke", [python, "scripts/platform_smoke.py"], env, BACKEND)))
        if "eval" in selected:
            results.append(("run_eval", run_suite(
                "run_eval", [python, str(REPO_ROOT / "eval" / "run_eval.py")], env, REPO_ROOT)))
        if args.keep_running:
            print(f"\n服务保持在 {base}（CS_CONFIG={config}）。"
                  "交互式运行会等你回车；被脚本/CI 拉起时（无 TTY）会一直挂着，靠 Ctrl+C 或终止进程退出。")
            _wait_forever()

        print("\n===== 汇总 =====")
        failed = 0
        for name, (status, rc, elapsed) in results:
            print(f"  {name:16s} {status:14s} {elapsed:6.1f}s")
            failed += rc != 0
        return 1 if failed else 0
    finally:
        if not args.keep_running:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:   # Windows 上 uvicorn 的子 worker 可能不响应 TERM
                server.kill()
                server.wait(timeout=10)
            print(f"\nCI 服务已关停，临时数据留在 {tmp}")


if __name__ == "__main__":
    raise SystemExit(main())
