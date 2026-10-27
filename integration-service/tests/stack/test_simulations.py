"""The simulations as automated tests against the running stack: ``pytest -m stack``."""

from collections.abc import Callable, Iterator

import pytest
from simulations.evidence import Evidence
from simulations.harness import Stack
from simulations.scenarios import SCENARIOS

pytestmark = pytest.mark.stack


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    live = Stack()
    yield live
    live.clear_faults()


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_simulation(stack: Stack, scenario: Callable[[Stack], Evidence]) -> None:
    try:
        evidence = scenario(stack)
    finally:
        stack.clear_faults()
    assert evidence.passed, evidence.to_markdown()
