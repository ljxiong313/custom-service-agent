"""loguru 日志初始化：控制台 + 按天滚动文件（data/logs/）。"""
import sys

from loguru import logger

from app.config import BACKEND_ROOT

LOG_DIR = BACKEND_ROOT / "data" / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()  # 移除 loguru 默认 handler，避免重复输出
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
                      "{name}:{function} - <level>{message}</level>")
    logger.add(LOG_DIR / "cs_agent_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days",
               encoding="utf-8", level="DEBUG")
