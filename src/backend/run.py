"""后端启动入口：host/port/workers 都从配置读，不用每次在命令行手抄。

    cd src/backend
    CS_CONFIG=config.local.yaml py run.py
    py run.py --port 7862                    # 临时改端口（并排起第二个实例做对比）
    CS_WORKERS=2 py run.py                   # 多副本（生产灰度/单机多核）

B3-8 起放开多 worker：MCP 工具确认、记忆管道任务都落了库，生命周期扫描与外发重试走
workerlease 租约选主，多进程不会重复提示、重复结单，也不会把确认请求送错进程。
仍然有几处是**每进程一份**的：BM25 索引缓存、MCP 工具发现缓存、限流令牌桶、HTTP 计数。
前者只影响首次检索延迟，后者只影响限流精度（限额要按副本数分摊）——
`workers` 不是越大越好，先读 docs/运维手册.md 的容量一节。

刻意不开 --reload：reload 会 fork 出子进程，本地调试时日志与后台任务的归属会变得难判断。
"""
from __future__ import annotations

import argparse
import os

import uvicorn

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="智能客服 Agent 后端")
    parser.add_argument("--host", default=settings.get_dotted("server.host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(settings.get_dotted("server.port", 7861)))
    parser.add_argument("--workers", type=int,
                       default=int(os.environ.get("CS_WORKERS")
                                   or settings.get_dotted("server.workers", 1)),
                       help="进程数；>1 时确认表/扫描/重试靠数据库与租约协调（见 docs/运维手册.md）")
    args = parser.parse_args()

    workers = max(1, args.workers)
    print(f"配置文件: {settings.get('_config_file')}")
    print(f"启动: http://{args.host}:{args.port}  文档 /docs  存活 /health  就绪 /ready  "
          f"探针 metrics /metrics  workers={workers}")
    if workers > 1:
        print("提示: 多 worker 下 server.rate_limit 的限额是**每进程**的，请按副本数折算阈值")
    # workers>1 时 uvicorn 不允许传 app 对象，必须用导入串（这里本来就是串）
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
