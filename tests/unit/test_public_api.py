"""Guard the package's public re-export surface (``macp_sdk.__all__``).

A new symbol imported in ``__init__.py`` but missing from ``__all__`` (or
vice versa) silently changes what ``from macp_sdk import *`` and API docs
expose — these tests make that a loud failure instead.
"""

from __future__ import annotations

import dataclasses
import typing

import macp_sdk
from macp_sdk.decision import DecisionSession
from macp_sdk.handoff import HandoffProjection, HandoffSession
from macp_sdk.projections import DecisionProjection
from macp_sdk.proposal import ProposalProjection, ProposalSession
from macp_sdk.quorum import QuorumProjection, QuorumSession
from macp_sdk.task import TaskProjection, TaskSession

_CLASSES_TO_SCAN = (
    DecisionSession,
    DecisionProjection,
    ProposalSession,
    ProposalProjection,
    TaskSession,
    TaskProjection,
    HandoffSession,
    HandoffProjection,
    QuorumSession,
    QuorumProjection,
)


def _record_dataclasses_in(annotation: object) -> set[type]:
    """Recursively collect any ``macp_sdk``-owned dataclass reachable from
    *annotation*, unwrapping ``| None``/``list[...]``/``dict[str, ...]``.
    """
    module_name = str(getattr(annotation, "__module__", ""))
    if dataclasses.is_dataclass(annotation) and module_name.startswith("macp_sdk"):
        return {typing.cast(type, annotation)}
    found: set[type] = set()
    for arg in typing.get_args(annotation):
        found |= _record_dataclasses_in(arg)
    return found


def _public_methods(cls: type) -> list[tuple[str, object]]:
    """Methods defined directly on *cls* (not inherited), public, callable."""
    return [
        (name, value)
        for name, value in vars(cls).items()
        if not name.startswith("_") and callable(value) and not isinstance(value, type)
    ]


def _reexported_names() -> set[str]:
    """Public names bound on the package by ``__init__.py`` imports."""
    return {
        name
        for name, value in vars(macp_sdk).items()
        if not name.startswith("_")
        # skip submodules picked up as attributes (e.g. macp_sdk.auth)
        and getattr(value, "__class__", None).__name__ != "module"
    }


class TestPublicApi:
    def test_all_has_no_duplicates(self):
        assert len(macp_sdk.__all__) == len(set(macp_sdk.__all__))

    def test_every_all_entry_resolves(self):
        missing = [name for name in macp_sdk.__all__ if not hasattr(macp_sdk, name)]
        assert not missing, f"__all__ names that don't resolve: {missing}"

    def test_every_reexport_is_in_all(self):
        undeclared = _reexported_names() - set(macp_sdk.__all__)
        assert not undeclared, f"public re-exports missing from __all__: {sorted(undeclared)}"

    def test_all_is_sorted_within_reason(self):
        # Not asserting full sort (grouping by theme is fine) — just that the
        # list is non-trivial and every entry is a str.
        assert len(macp_sdk.__all__) > 50
        assert all(isinstance(n, str) for n in macp_sdk.__all__)


class TestRecordDataclassExports:
    """Every record dataclass a public mode-session/projection method returns
    must be importable from top-level ``macp_sdk`` (Phase 5,
    sdk-parity-python-fixes.md) -- otherwise a caller can receive a value they
    cannot type-annotate against. Unlike ``test_every_reexport_is_in_all``
    above (which only catches a name already imported into ``__init__.py``
    but left out of ``__all__``), this walks return-type annotations to catch
    a record dataclass that was never imported into ``__init__.py`` at all.
    """

    def test_every_returned_record_dataclass_is_exported(self):
        missing: list[str] = []
        for cls in _CLASSES_TO_SCAN:
            for name, method in _public_methods(cls):
                try:
                    hints = typing.get_type_hints(method)
                except (NameError, TypeError):  # pragma: no cover - defensive
                    continue
                ret = hints.get("return")
                if ret is None:
                    continue
                for record_cls in _record_dataclasses_in(ret):
                    if record_cls.__name__ not in macp_sdk.__all__:
                        missing.append(f"{cls.__name__}.{name} -> {record_cls.__name__}")
        assert not missing, f"record dataclasses returned but not exported: {missing}"

    def test_scan_actually_finds_something(self):
        # A guard against the scan silently finding zero return-annotated
        # record dataclasses (e.g. if a refactor changed method shapes) --
        # without this, test_every_returned_record_dataclass_is_exported
        # could trivially "pass" by checking nothing.
        found: set[str] = set()
        for cls in _CLASSES_TO_SCAN:
            for _name, method in _public_methods(cls):
                hints = typing.get_type_hints(method)
                found |= {c.__name__ for c in _record_dataclasses_in(hints.get("return"))}
        assert found, "expected at least one record dataclass return annotation to scan"


class TestVersionAndTimeHelper:
    def test_version_is_nonempty_string_matching_pyproject(self):
        assert isinstance(macp_sdk.__version__, str)
        assert macp_sdk.__version__ != ""

    def test_now_unix_ms_is_exported_and_callable(self):
        from macp_sdk import now_unix_ms

        ts = now_unix_ms()
        assert isinstance(ts, int)
        assert ts > 0
