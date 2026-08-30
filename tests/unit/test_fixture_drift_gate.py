"""Drive the real ``make sync-fixtures`` / ``make verify-fixtures`` recipes
against synthetic trees, proving the ``FIXTURE_DIR_PAIRS``-driven rewrite
(``Makefile``) covers both ``tests/conformance/`` (pre-existing behaviour)
and ``tests/vectors/cmt-hash/`` (new -- see issue #38 and
``tests/vectors/cmt-hash/SOURCE.md``) without needing the real spec repo
checked out.

Every case builds a throwaway "repo" (just the two fixture directories the
Makefile cares about) and a throwaway "canonical" tree, then invokes
``make -f <this repo's Makefile> <target> SPEC_CONFORMANCE_DIR=<canonical>``
with ``cwd`` set to the throwaway repo. This exercises the actual shell
recipe -- not a reimplementation of its logic -- so a regression in the
Makefile itself (a stray ``exit``, a mis-escaped ``$$``, a pipeline that
introduces a subshell and drops the ``drift`` accumulator) is caught here
rather than only in CI against the real spec repo.

Invocation contract, and why each piece matters:

- ``cwd=`` is used, never ``make -C``: ``-C`` implies ``-w``, which prints
  ``Entering/Leaving directory`` to stdout on GNU Make 4.x (ubuntu-latest)
  but not 3.81 (macOS) -- that would make any stdout assertion here
  non-portable across CI and local runs.
- The ``-f`` path is absolute (``Makefile`` at the real repo root), because
  with ``cwd=`` pointed at the synthetic tree a relative ``-f`` would
  resolve against the wrong directory.
- ``MAKEFLAGS``/``MAKELEVEL``/``MFLAGS`` are stripped from the subprocess
  env. ``make test`` runs this file under pytest under ``make`` -- a nested
  invocation -- and those three vars are exported by the outer ``make``.
  Left in place they change Make's own error-line prefix to ``make[1]:`` and
  can leak outer flags (e.g. a jobserver from ``make -j4 test``) into the
  child, adding stderr noise unrelated to what a case is asserting.
  Regular test invocations (bare ``pytest``) do not set them, so this can't
  turn a passing case into a false pass -- it only defends the ``make test``
  path.
- Assertions on failure are always ``returncode != 0``, never ``== 1``: GNU
  Make exits with status **2** when a recipe fails (via ``exit 1`` or an
  unguarded nonzero command), regardless of the recipe's own exit code.
- The ``DRIFT``/``EXTRA``/``MISSING``/``OK`` lines the recipes print go to
  stdout; Make's own ``*** [target] Error N`` line goes to stderr. The two
  streams are captured separately (never merged) and every assertion here
  reads only ``result.stdout``.

This module imports nothing from ``macp_sdk`` and needs no running MACP
runtime, so it also executes cleanly under
``.github/workflows/proto-drift.yml``'s ``pytest tests/unit tests/conformance -q``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

# Doesn't distinguish GNU make from BSD make -- the recipes rely on GNU-only
# shell-in-recipe semantics (e.g. `$$` escaping applied consistently across
# a `\`-continued recipe line). Both ubuntu-latest and macOS ship GNU make,
# so this is enough to skip only on a genuinely make-less machine.
pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not found on PATH")


def _write_files(directory: Path, files: dict[str, str] | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (directory / name).write_text(content, encoding="utf-8")


def _make_canon(
    tmp_path: Path,
    *,
    flat: dict[str, str] | None = None,
    cmt_hash: dict[str, str] | None = None,
    include_cmt_hash_dir: bool = True,
) -> Path:
    """Build a throwaway canonical tree mirroring
    ``$(SPEC_CONFORMANCE_DIR)``: a flat top level plus an optional
    ``cmt-hash/`` subdirectory."""
    canon = tmp_path / "canon"
    _write_files(canon, flat)
    if include_cmt_hash_dir:
        _write_files(canon / "cmt-hash", cmt_hash)
    return canon


def _make_repo(
    tmp_path: Path,
    *,
    conformance: dict[str, str] | None = None,
    cmt_hash: dict[str, str] | None = None,
    create_cmt_hash_dir: bool = True,
) -> Path:
    """Build a throwaway repo tree holding just the two directories the
    Makefile's ``FIXTURE_DIR_PAIRS`` names: ``tests/conformance`` and
    ``tests/vectors/cmt-hash``."""
    repo = tmp_path / "repo"
    _write_files(repo / "tests" / "conformance", conformance)
    if create_cmt_hash_dir:
        _write_files(repo / "tests" / "vectors" / "cmt-hash", cmt_hash)
    return repo


def _run_make(repo: Path, target: str, spec_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}}
    return subprocess.run(
        ["make", "-f", str(MAKEFILE), target, f"SPEC_CONFORMANCE_DIR={spec_dir}"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# 1. Both pairs identical.
def test_verify_passes_when_both_pairs_identical(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={"b.json": "B"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode == 0
    assert "All conformance fixtures match the canonical source." in result.stdout
    assert "OK: tests/conformance matches" in result.stdout
    assert "OK: tests/vectors/cmt-hash matches" in result.stdout
    assert "DRIFT" not in result.stdout
    assert "EXTRA" not in result.stdout
    assert "MISSING" not in result.stdout


# 2. cmt-hash vendored content differs.
def test_verify_flags_drift_in_cmt_hash_content(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "canonical"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={"b.json": "stale"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "DRIFT: tests/vectors/cmt-hash/b.json" in result.stdout


# 3. cmt-hash canonical file absent locally (upstream addition).
def test_verify_flags_drift_when_cmt_hash_file_missing_locally(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"new.json": "content"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "DRIFT: tests/vectors/cmt-hash/new.json" in result.stdout


# 4. Local-only tests/vectors/cmt-hash/extra.json.
def test_verify_flags_extra_cmt_hash_file(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(
        tmp_path, conformance={"a.json": "A"}, cmt_hash={"b.json": "B", "extra.json": "X"}
    )

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "EXTRA: tests/vectors/cmt-hash/extra.json" in result.stdout


# 5. Local-only SOURCE.md alongside an otherwise clean cmt-hash pair.
def test_verify_ignores_source_md(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={"b.json": "B"})
    source_md = repo / "tests" / "vectors" / "cmt-hash" / "SOURCE.md"
    source_md.write_text("provenance", encoding="utf-8")

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode == 0
    assert "SOURCE.md" not in result.stdout


# 6. tests/conformance/ content drift (no regression of existing behaviour).
def test_verify_flags_drift_in_conformance_content(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "canonical"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "stale"}, cmt_hash={"b.json": "B"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "DRIFT: tests/conformance/a.json" in result.stdout
    # Ordinary drift (no MISSING canonical directory involved) must not
    # print the sync-fixtures-can't-fix-this note -- that's reserved for
    # the MISSING case (see M2 / test 8 below).
    assert "Note: sync-fixtures can't fix MISSING" not in result.stdout


# 7. tests/conformance/ extra file (no regression of existing behaviour).
def test_verify_flags_extra_conformance_file(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(
        tmp_path, conformance={"a.json": "A", "extra.json": "X"}, cmt_hash={"b.json": "B"}
    )

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "EXTRA: tests/conformance/extra.json" in result.stdout


# 8. Canonical cmt-hash/ subdirectory missing entirely -- must still report
#    on the tests/conformance pair (accumulate, don't exit mid-loop).
def test_verify_reports_missing_canonical_subdir_and_still_checks_other_pair(
    tmp_path: Path,
) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, include_cmt_hash_dir=False)
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={"b.json": "B"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert f"MISSING: canonical directory {canon}/cmt-hash does not exist" in result.stdout
    assert "OK: tests/conformance matches" in result.stdout
    # M2: a MISSING canonical directory can't be fixed by sync-fixtures, so
    # the failure summary must carry the extra remedy note.
    assert "Note: sync-fixtures can't fix MISSING" in result.stdout


# 9. Multi-pair accumulation: drift in both pairs in one run.
def test_verify_accumulates_drift_across_both_pairs(tmp_path: Path) -> None:
    canon = _make_canon(
        tmp_path, flat={"a.json": "canonical-a"}, cmt_hash={"b.json": "canonical-b"}
    )
    repo = _make_repo(tmp_path, conformance={"a.json": "stale-a"}, cmt_hash={"b.json": "stale-b"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "DRIFT: tests/conformance/a.json" in result.stdout
    assert "DRIFT: tests/vectors/cmt-hash/b.json" in result.stdout


# 10. SPEC_CONFORMANCE_DIR points at a nonexistent path (pre-existing guard).
def test_verify_fails_when_spec_dir_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    missing_spec_dir = tmp_path / "does-not-exist"

    result = _run_make(repo, "verify-fixtures", missing_spec_dir)

    assert result.returncode != 0
    assert f"spec repo not found at {missing_spec_dir}" in result.stdout


# 11. sync-fixtures populates cmt-hash from canonical; a following verify passes.
def test_sync_then_verify_is_clean(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={})

    sync_result = _run_make(repo, "sync-fixtures", canon)
    verify_result = _run_make(repo, "verify-fixtures", canon)

    assert sync_result.returncode == 0
    assert verify_result.returncode == 0
    assert (repo / "tests" / "vectors" / "cmt-hash" / "b.json").read_text(encoding="utf-8") == "B"


# 12. sync-fixtures creates tests/vectors/cmt-hash/ when it doesn't exist.
def test_sync_creates_missing_cmt_hash_dir(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, create_cmt_hash_dir=False)
    cmt_hash_dir = repo / "tests" / "vectors" / "cmt-hash"
    assert not cmt_hash_dir.exists()

    result = _run_make(repo, "sync-fixtures", canon)

    assert result.returncode == 0
    assert (cmt_hash_dir / "b.json").read_text(encoding="utf-8") == "B"


# 13. sync-fixtures does not delete a local EXTRA file.
def test_sync_does_not_delete_extra_file(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={"extra.json": "still here"})
    extra_path = repo / "tests" / "vectors" / "cmt-hash" / "extra.json"

    sync_result = _run_make(repo, "sync-fixtures", canon)
    verify_result = _run_make(repo, "verify-fixtures", canon)

    assert sync_result.returncode == 0
    assert extra_path.exists()
    assert extra_path.read_text(encoding="utf-8") == "still here"
    assert verify_result.returncode != 0
    assert "EXTRA: tests/vectors/cmt-hash/extra.json" in verify_result.stdout


# 14. sync-fixtures pre-flight MISSING hard-fail: a canonical subdirectory
#     absent must abort before any copying happens (not merely report drift).
def test_sync_fails_before_copying_when_cmt_hash_canonical_missing(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, include_cmt_hash_dir=False)
    repo = _make_repo(tmp_path, conformance={}, cmt_hash={})
    conformance_dir = repo / "tests" / "conformance"

    result = _run_make(repo, "sync-fixtures", canon)

    assert result.returncode != 0
    assert f"MISSING: canonical directory {canon}/cmt-hash does not exist" in result.stdout
    # Nothing was copied -- the pre-flight loop must fail *before* the
    # separate copy loop runs, even though the "." pair's canonical dir
    # (with a.json) is present and would otherwise be copied.
    assert list(conformance_dir.iterdir()) == []


# 15. Canonical-side empty-glob guard: an empty canonical cmt-hash/ directory
#     (unexpanded "*.json" glob) must never be treated as a literal filename.
def test_verify_ok_when_cmt_hash_pair_is_empty_on_both_sides(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "A"}, cmt_hash={})
    repo = _make_repo(tmp_path, conformance={"a.json": "A"}, cmt_hash={})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode == 0
    assert "OK: tests/vectors/cmt-hash matches" in result.stdout
    assert "DRIFT" not in result.stdout


# 16. A drifted pair does not suppress another, clean pair's OK line, and
#     does not leak a DRIFT/EXTRA line for the clean pair (pins pair_drift
#     against leaking between pairs).
def test_verify_drift_in_one_pair_does_not_suppress_other_pairs_ok(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path, flat={"a.json": "canonical"}, cmt_hash={"b.json": "B"})
    repo = _make_repo(tmp_path, conformance={"a.json": "stale"}, cmt_hash={"b.json": "B"})

    result = _run_make(repo, "verify-fixtures", canon)

    assert result.returncode != 0
    assert "DRIFT: tests/conformance/a.json" in result.stdout
    assert "OK: tests/vectors/cmt-hash matches" in result.stdout
    assert "DRIFT: tests/vectors/cmt-hash" not in result.stdout
    assert "EXTRA:" not in result.stdout


# 17. make help still lists both targets, with no row for FIXTURE_DIR_PAIRS.
def test_help_lists_fixture_targets_without_leaking_the_pairs_variable() -> None:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}}
    result = subprocess.run(
        ["make", "-f", str(MAKEFILE), "help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0
    assert "sync-fixtures" in result.stdout
    assert "verify-fixtures" in result.stdout
    assert "FIXTURE_DIR_PAIRS" not in result.stdout
