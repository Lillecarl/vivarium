import pytest


@pytest.fixture
def anyio_backend() -> str:
    """asyncio only. The mechanism underneath uses `loop.add_reader` and
    an asyncio subprocess, so trio is not a backend this can run on."""
    return "asyncio"
