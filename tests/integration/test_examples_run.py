"""Execute every ``examples/*.py`` script against a live runtime (#49).

``tests/unit/test_examples_smoke.py`` only ``compile()``s the examples, which
catches syntax rot but nothing else -- a renamed API (``got.descriptor`` vs
``got.policy_descriptor``) or a call missing a required argument
(``proj.is_completed()``) compiles cleanly and only fails at runtime. This
module is the gate that actually runs them.

Examples are run as **subprocesses**, not imported: several execute
top-level I/O against a real ``MacpClient`` at module scope, so importing
them in-process would open live gRPC channels inside the pytest process and
trip ``pyproject.toml``'s ``filterwarnings = ["error", ...]`` on a leaked
socket. A subprocess gives clean isolation and a real traceback in
``stderr`` for failure messages.

Two mechanisms keep this gate from silently narrowing as examples are
added or removed:

1. ``EXCLUDED`` is an explicit ``dict[str, str]`` of filename -> reason.
   Every example not in ``EXCLUDED`` is run.
2. ``test_coverage_parity`` asserts ``RUN | EXCLUDED.keys()`` covers exactly
   ``{p.name for p in EXAMPLES}``. A newly added ``examples/*.py`` with no
   ``EXCLUDED`` entry fails this test until someone classifies it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.conftest import RUNTIME_TARGET

pytestmark = pytest.mark.integration

EXAMPLES_DIR = Path(__file__).parents[2] / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))

# filename -> why it is not run by this gate. Every entry must have a
# non-empty reason (see test_excluded_reasons_are_non_empty below).
EXCLUDED: dict[str, str] = {
    "direct_agent_auth_observer.py": (
        "requires a concurrently running direct_agent_auth_initiator.py "
        "sharing MACP_SESSION_ID; it blocks on stream.responses(timeout=5.0) "
        "and exits only on a Commitment from that paired process. The "
        "two-process handshake is covered by "
        "tests/integration/test_direct_agent_auth.py instead."
    ),
}

# Deliberately hardcoded rather than derived from EXAMPLES/EXCLUDED: if RUN
# were computed as "everything not in EXCLUDED", test_coverage_parity below
# would be a tautology and a brand-new examples/*.py would silently join RUN
# instead of failing the gate. Listing the known-good files here is what
# makes an unclassified addition a hard failure.
RUN = [
    "agent_policy_aware.py",
    "decision_smoke.py",
    "direct_agent_auth_initiator.py",
    "handoff_escalation.py",
    "policy_registration.py",
    "proposal_negotiation.py",
    "quorum_approval.py",
    "task_delegation.py",
]

# Generous relative to the whole existing integration suite (~3s); guards
# against a hang (e.g. a blocking stream read) taking CI down with it.
TIMEOUT_SECS = 60.0


def _run_example(name: str) -> subprocess.CompletedProcess[str]:
    path = EXAMPLES_DIR / name
    env = {**os.environ, "MACP_RUNTIME_TARGET": RUNTIME_TARGET}
    try:
        return subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{name} timed out after {TIMEOUT_SECS}s (stdout so far:\n"
            f"{exc.stdout}\nstderr so far:\n{exc.stderr})"
        )


# All examples are parametrized here -- excluded ones carry an explicit
# pytest.mark.skip(reason=...) so the exclusion is visible in test output
# (e.g. "8 passed, 1 skipped") instead of silently vanishing from collection.
_ALL_PARAMS = [
    pytest.param(p.name, marks=pytest.mark.skip(reason=EXCLUDED[p.name]))
    if p.name in EXCLUDED
    else pytest.param(p.name)
    for p in EXAMPLES
]


@pytest.mark.parametrize("name", _ALL_PARAMS)
def test_example_runs(name: str) -> None:
    result = _run_example(name)
    assert result.returncode == 0, (
        f"{name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def test_coverage_parity() -> None:
    """Every example is either run or explicitly excluded with a reason.

    A new ``examples/*.py`` file that is neither in ``RUN`` nor in
    ``EXCLUDED`` fails this test -- it cannot silently escape the gate.
    """
    all_names = {p.name for p in EXAMPLES}
    covered = set(RUN) | EXCLUDED.keys()
    assert covered == all_names, (
        f"examples not covered by RUN or EXCLUDED: {all_names - covered}; "
        f"stale entries no longer on disk: {covered - all_names}"
    )


def test_excluded_reasons_are_non_empty() -> None:
    for name, reason in EXCLUDED.items():
        assert reason.strip(), f"EXCLUDED[{name!r}] has an empty reason"


def test_examples_directory_found() -> None:
    assert len(EXAMPLES) >= 9, f"expected >=9 example scripts in {EXAMPLES_DIR}, found {EXAMPLES}"
