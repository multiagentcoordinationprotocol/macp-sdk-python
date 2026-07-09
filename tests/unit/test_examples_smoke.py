"""Compile-only smoke tests for ``examples/``.

Several examples create a ``MacpClient`` at module top level, so importing
them would attempt network I/O — ``compile()`` catches syntax rot (renamed
APIs won't be caught here, but the examples at least stay parseable and are
exercised for real against a runtime in the docs/release flow).
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2] / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))


def test_examples_directory_found():
    assert len(EXAMPLES) >= 9, f"expected >=9 example scripts in {EXAMPLES_DIR}, found {EXAMPLES}"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_compiles(path: Path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
