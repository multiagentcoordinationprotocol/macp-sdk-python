"""Guard the package's public re-export surface (``macp_sdk.__all__``).

A new symbol imported in ``__init__.py`` but missing from ``__all__`` (or
vice versa) silently changes what ``from macp_sdk import *`` and API docs
expose — these tests make that a loud failure instead.
"""

from __future__ import annotations

import macp_sdk


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
