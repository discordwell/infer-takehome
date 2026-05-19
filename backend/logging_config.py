from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import settings


def configure_logging() -> None:
    log_path = Path(settings.log_file_path)
    if not log_path.is_absolute():
        log_path = Path(__file__).resolve().parent.parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(min(root_logger.level or level, level))
    if not _has_file_handler(root_logger, log_path):
        root_logger.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(min(logger.level or level, level))
        if not logger.propagate and not _has_file_handler(logger, log_path):
            logger.addHandler(handler)


def _has_file_handler(logger: logging.Logger, log_path: Path) -> bool:
    resolved = log_path.resolve()
    for handler in logger.handlers:
        if not isinstance(handler, RotatingFileHandler):
            continue
        try:
            if Path(handler.baseFilename).resolve() == resolved:
                return True
        except OSError:
            continue
    return False
