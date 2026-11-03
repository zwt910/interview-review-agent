"""
统一日志初始化：控制台 + 滚动文件，便于本地排查。

日志目录默认：项目根/logs/
  - app.log      业务与流水线
  - access.log   HTTP 访问（由中间件写入）
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from config.llm_config import PROJECT_ROOT

_INITIALIZED = False


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
    force: bool = False,
) -> Path:
    """初始化根日志与 access 日志。可重复调用（默认只生效一次）。"""
    global _INITIALIZED
    if _INITIALIZED and not force:
        return Path(log_dir or PROJECT_ROOT / "logs")

    log_path = Path(log_dir or PROJECT_ROOT / "logs")
    log_path.mkdir(parents=True, exist_ok=True)

    log_level = getattr(logging, (level or "INFO").upper(), logging.INFO)
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(log_level)
    # 清理旧 handler，避免重复启动叠加
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    app_file = RotatingFileHandler(
        log_path / "app.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    app_file.setLevel(log_level)
    app_file.setFormatter(fmt)
    root.addHandler(app_file)

    # 访问日志单独文件，避免和业务日志搅在一起
    access_logger = logging.getLogger("access")
    access_logger.setLevel(log_level)
    access_logger.propagate = False
    for h in list(access_logger.handlers):
        access_logger.removeHandler(h)
    access_file = RotatingFileHandler(
        log_path / "access.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    access_file.setFormatter(fmt)
    access_console = logging.StreamHandler(sys.stdout)
    access_console.setFormatter(fmt)
    access_logger.addHandler(access_file)
    access_logger.addHandler(access_console)

    # 压低第三方噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _INITIALIZED = True
    logging.getLogger(__name__).info(
        "日志已初始化 level=%s dir=%s files=app.log,access.log",
        logging.getLevelName(log_level),
        log_path,
    )
    return log_path
