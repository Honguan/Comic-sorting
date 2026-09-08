"""Bounded UTF-8 diagnostic logs without credentials or config contents."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys


logger = logging.getLogger("comic_sorting")
logger.addHandler(logging.NullHandler())


def log_path():
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return Path(handler.baseFilename)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / "logs" / "comic-sorting.log"


def redact(text):
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[a-z0-9._~+/=-]+", r"\1 <REDACTED>", str(text))
    text = re.sub(
        r"(?i)(\b(?:api[_ -]?key|access[_ -]?token|authorization|password|secret)\b[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", r"\1<REDACTED>", text)
    return re.sub(r"\bsk-[a-zA-Z0-9_-]{12,}\b", "<REDACTED>", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def configure_logging(path=None):
    path = Path(path) if path is not None else log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return path
