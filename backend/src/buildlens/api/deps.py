"""Request-scoped dependencies for route handlers.

The session comes from the factory the lifespan built, so every request
borrows from one pooled engine rather than constructing its own. Routes commit
explicitly: FastAPI runs a yield dependency's teardown after the handler
returns, so committing there would put a failure after the response was
already decided. An uncommitted session rolls back when this context exits,
which is what makes a raised exception safe.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
