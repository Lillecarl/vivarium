from collections.abc import AsyncIterator

import pytest
from vivarium_runner import Machine


@pytest.fixture
async def marker(one: Machine) -> AsyncIterator[str]:
    """Setup and teardown both on the guest, so the check can see both ran."""
    await one.succeed("echo up > /artifacts/fixture-setup")
    yield "marked"
    await one.succeed("echo down > /artifacts/fixture-teardown")
