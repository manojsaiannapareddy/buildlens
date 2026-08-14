"""Worker process entrypoint."""

import asyncio
import signal

import structlog

from buildlens.core.config import get_settings
from buildlens.core.logging_setup import configure_logging
from buildlens.core.scheduler import run_scheduler
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
        # Both loops share one shutdown event, so a single SIGTERM stops the
        # process cleanly. TaskGroup means a genuine crash in either one is
        # loud rather than silently leaving half a worker running.
        async with asyncio.TaskGroup() as group:
            group.create_task(run_worker(session_factory, shutdown))
            group.create_task(run_scheduler(session_factory, shutdown))
    finally:
        await engine.dispose()
        logger.info("worker.shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
