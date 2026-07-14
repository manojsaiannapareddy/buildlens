"""Structured logging (spec §14.1): pretty console in dev, JSON to stdout in prod.

Configured once at startup, driven by Settings. Everything else just does:
    logger = structlog.get_logger()
    logger.info("event.name", key=value)
"""

import logging

import structlog

from buildlens.core.config import Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if settings.environment == "prod":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )