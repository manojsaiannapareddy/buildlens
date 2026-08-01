"""Worker process entrypoint."""

import asyncio
import signal

import structlog

from buildlens.core.config import get_settings
from buildlens.core.logging_setup import configure_logging
from buildlens.core.worker import run_worker
from buildlens.db.session import create_engine, create_session_factory


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logger = structlog.get_logger()

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    logger.info("worker.startup_complete", environment=settings.environment)
    try:
        await run_worker(session_factory, shutdown)
    finally:
        await engine.dispose()
        logger.info("worker.shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
