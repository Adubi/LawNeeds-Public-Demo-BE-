import json
import logging
import os
from datetime import datetime, timezone


STANDARD_LOG_RECORD_KEYS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.getenv("SERVICE_NAME", "lawneeds-api"),
            "environment": os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")),
        }

        for key, value in record.__dict__.items():
            if key not in STANDARD_LOG_RECORD_KEYS and not key.startswith("_"):
                log_record[key] = self._json_safe(value)

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record, default=str, ensure_ascii=False)

    @staticmethod
    def _json_safe(value):
        try:
            json.dumps(value, default=str)
            return value
        except TypeError:
            return str(value)


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # APScheduler logs every routine job start/success at INFO, which is noisy for
    # high-frequency production jobs. Keep warnings/errors visible by default.
    apscheduler_level = os.getenv("APSCHEDULER_LOG_LEVEL", "WARNING").upper()
    logging.getLogger("apscheduler").setLevel(apscheduler_level)
    logging.getLogger("apscheduler.executors.default").setLevel(apscheduler_level)
