import logging
import logging.handlers
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).parent
LOG_DIR = Path(os.environ.get("LOG_DIR", str(ROOT_DIR / "logs")))

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ACCESS_LOGGER_NAME = "convoy.access"
APP_LOGGER_NAME = "convoy"


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure console + rotating file handlers. Call once at startup."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    if getattr(setup_logging, "_configured", False):
        return logging.getLogger(APP_LOGGER_NAME)
    setup_logging._configured = True

    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    all_file = logging.handlers.RotatingFileHandler(
        LOG_DIR / "convoy.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    all_file.setLevel(logging.DEBUG)
    all_file.setFormatter(formatter)
    root.addHandler(all_file)

    error_file = logging.handlers.RotatingFileHandler(
        LOG_DIR / "error.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(formatter)
    root.addHandler(error_file)

    access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False
    access_logger.handlers.clear()

    access_file = logging.handlers.RotatingFileHandler(
        LOG_DIR / "access.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    access_file.setFormatter(formatter)
    access_logger.addHandler(access_file)

    access_console = logging.StreamHandler(sys.stdout)
    access_console.setFormatter(formatter)
    access_logger.addHandler(access_console)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.info("Logging initialized - files: %s", LOG_DIR.resolve())
    return app_logger


def get_access_logger() -> logging.Logger:
    return logging.getLogger(ACCESS_LOGGER_NAME)
