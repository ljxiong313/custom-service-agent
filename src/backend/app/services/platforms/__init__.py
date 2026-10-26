"""外部电商平台适配层（P7）。

每个平台一个模块，统一实现 base.Adapter：
    verify_signature()  回调验签（mock 模式放行，便于本地联调）
    parse_message()     回调载荷 → InboundMessage
    send_reply()        回复买家（真实走平台 HTTP API，mock/skipped/failed 一律落外发箱）
    send_media()        图片/文件出站（media_method 未配置时降级为 [附件] 链接文本）
inbound.py 负责映射影子用户/会话并驱动 runner.run_turn 完成自动回复，
配置就绪度自检见 base.readiness()（GET /api/v1/platforms/readiness）。

真实对接前必须逐项核对 docs/平台接入.md 的参数与方法名（平台文档随版本变化）。

注意：导入 base 不会注册适配器；API 层需 import app.services.platforms.adapters 触发注册。
"""
from .base import ADAPTERS, PLATFORM_ORDER, InboundMessage, conf, get_adapter, is_mock, outbox_records, readiness  # noqa: F401
