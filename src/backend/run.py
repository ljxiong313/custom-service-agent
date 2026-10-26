"""后端启动入口：host/port 从配置文件读，不用每次在命令行手抄。

    cd src/backend
    CS_CONFIG=config.local.yaml py run.py
    py run.py --port 7862              # 只临时改端口（并排起第二个实例做对比）

刻意不提供 --reload 和 --workers：reload 会 fork 出 worker 进程，多 worker 更是每进程
各跑一遍 sweeper、各自持有一份 MCP 确认表（app/main.py 的部署形态说明），所以这里只
暴露 host/port 两项，其余交给配置。用 `py -m uvicorn` 手工启动仍然可以，但那要求你
自己保证端口和 worker 数一致——日常用本文件。
"""
from __future__ import annotations

import argparse

import uvicorn

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="智能客服 Agent 后端（单 worker）")
    parser.add_argument("--host", default=settings.get_dotted("server.host", "127.0.0.1"),
                        help="默认取配置里的 server.host")
    parser.add_argument("--port", type=int, default=int(settings.get_dotted("server.port", 7861)),
                        help="默认取配置里的 server.port")
    args = parser.parse_args()

    print(f"配置文件: {settings.get('_config_file')}")
    print(f"启动: http://{args.host}:{args.port}  文档 /docs  健康检查 /health")
    uvicorn.run("app.main:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
