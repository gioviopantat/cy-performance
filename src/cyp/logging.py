"""structlog configuration: JSON lines to ``{CYP_DATA_DIR}/logs/cyp.jsonl`` + console renderer.

Call :func:`configure_logging` once at process start (the CLI does). Modules obtain loggers via
``structlog.get_logger(__name__)``.
"""

from __future__ import annotations

import logging as _stdlib_logging
import sys
from pathlib import Path

import structlog

LOG_FILENAME = "cyp.jsonl"
_CONFIGURED_FOR: Path | None = None


def _shared_processors() -> list[structlog.types.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    logs_dir: Path,
    *,
    level: int = _stdlib_logging.INFO,
    console: bool = True,
    force: bool = False,
) -> Path:
    """Route structlog + stdlib logging to a JSONL file and (optionally) the console.

    Returns the log file path. Re-configuring for the same directory is a no-op unless
    ``force`` is set.
    """
    global _CONFIGURED_FOR
    logs_dir = Path(logs_dir)
    log_path = logs_dir / LOG_FILENAME
    if logs_dir == _CONFIGURED_FOR and not force:
        return log_path
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = _stdlib_logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)

    file_handler = _stdlib_logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
            foreign_pre_chain=_shared_processors(),
        )
    )
    root.addHandler(file_handler)

    if console:
        console_handler = _stdlib_logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                ],
                foreign_pre_chain=_shared_processors(),
            )
        )
        root.addHandler(console_handler)

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    _CONFIGURED_FOR = logs_dir
    return log_path


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Named structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
