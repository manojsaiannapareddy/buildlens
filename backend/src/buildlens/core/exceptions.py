"""Shared domain exceptions.

Lives below worker and handlers so both can import it without a cycle.
This module must not import from anywhere else in core.
"""

from datetime import datetime


class TaskDeferred(Exception):
    """Raised by a handler when work isn't possible yet — not a failure.

    Carries the time after which the task should be retried (e.g. a rate-limit
    reset). Deferral does not consume a retry attempt.
    """

    def __init__(self, until: datetime) -> None:
        super().__init__(f"Deferred until {until.isoformat()}")
        self.until = until
