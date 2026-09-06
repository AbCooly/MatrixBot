"""统一日志模块：控制台 + 文件双输出，支持任务维度上下文。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def _init_logger(log_level: str = "INFO", log_dir: Path | None = None) -> logging.Logger:
    """初始化根日志器：控制台(INFO 起) + 文件(DEBUG 起，滚动 5MB×3)。

    默认写项目根 logs/agent.log（WebUI 的"运行日志"面板读取该文件）。
    """
    logger = logging.getLogger("social_agent")
    if logger.handlers:  # 避免重复初始化
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件输出（带滚动）：默认 logs/agent.log（项目根）
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "agent.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


# 模块级单例，供全项目使用
log = _init_logger()
