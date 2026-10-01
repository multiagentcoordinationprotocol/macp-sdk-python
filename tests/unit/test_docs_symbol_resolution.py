"""Permanent guard against doc-symbol-transcription drift (issue #117).

Three confirmed instances of the same bug class shipped before this guard
existed: ``task.md`` (#108), ``proposal.md`` (#106), and ``handoff.md`` (#111)
each transcribed the Rust runtime's internal struct field names/casing
verbatim into this SDK's public docs, documenting a Python API that did not
actually exist. This module parses every ``docs/modes/*.md`` file (plus two
files outside ``docs/modes/`` that also reference a projection -- see
``EXTRA_CHECK1_FILES``) and asserts:

- Check 1: every ``proj.<symbol>`` / ``proj.<coll>[...].field`` reference
  actually resolves against the real projection/record classes.
- Check 2: every ``*Record``/``*Projection``/``*Session``/``*Payload``/
  ``*Anomaly`` type name mentioned anywhere -- including inside ``#``
  comments, which Check 1 cannot see -- is a real symbol.
- Check 3: a documented ``proj.phase # "A" | "B" | ...`` value domain matches
  the code's actual set of reachable phase strings.
- Check 4: the extractor+resolver pipeline itself is non-vacuous (a
  self-test that would catch a regex/resolver regression that silently
  stops matching anything).

No hardcoded symbol allowlist and no hardcoded doc-to-class table: the
mode -> projection-class mapping is derived from every ``BaseSession``
subclass's single ``*_projection`` property and its return annotation, so a
sixth mode is picked up automatically. See the plan
(``plans/doc-symbol-guard-and-terminal-rejected-fix-117-118-119.md``,
Phase 1) for the full design rationale (decisions D1-D6).

Known, deliberate limitations (not bugs in this guard -- see the plan's
Phase 1 "Edge cases" for the evidence each is not currently reachable):

- Local-alias chains (``report = proj.failures[-1]``; ``report.error_code``)
  are not tracked -- this guard is purely ``proj.``-rooted. All 13
  historically drifted references were ``proj.``-rooted.
- Only the *last* attribute of a chain is checked (``proj.a.b.c`` checks
  ``a`` and ``c``, not ``b``). Not present in any doc today.
- A "direct" chain (no intervening ``[...]``/``(...)``) whose head resolves
  to ``None`` on a fresh instance (e.g. ``commitment``) cannot have its
  trailing attribute resolved via live-object traversal. Not exercised by
  any doc today; the annotation-based path already used for subscripted/
  called chains would need to be reused here if it ever is.
- Check 3 is severable: if it proves brittle, it may be dropped without
  weakening Checks 1 and 2 (recorded in the plan's Phase 1 Approach).
- Check 2 deliberately resolves names via ``vars(module)`` membership, never
  ``getattr(module, name)`` -- ``macp_sdk.proposal`` defines a module-level
  ``__getattr__`` that emits a ``DeprecationWarning`` for the pre-#103-rename
  aliases, and this repo's ``pyproject.toml`` turns warnings into test
  errors (``filterwarnings = ["error", ...]``).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from macp.modes.handoff.v1 import handoff_pb2
from macp.v1 import core_pb2

import macp_sdk
import macp_sdk.base_projection as base_projection_module
from macp_sdk.base_session import BaseSession

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_MODES_DIR = REPO_ROOT / "docs" / "modes"

# D5: these two files are outside docs/modes/ but bind a projection
# (`proj = session.decision_projection`) and reference `proj.*` just like a
# mode doc does. Free to include in Check 1 (anchored on the `proj.`
# receiver, so no false positives); excluded from Check 2 (bare type-name
# grep, which would false-positive on docs/architecture.md's gRPC RPC-method
# table -- see Check 2's docstring below).
EXTRA_CHECK1_FILES = [
    REPO_ROOT / "docs" / "architecture.md",
    REPO_ROOT / "docs" / "guides" / "building-orchestrators.md",
]

CAMEL_TYPE_RE = re.compile(r"\b[A-Z]\w*(?:Record|Projection|Session|Payload|Anomaly)\b")
PROJ_CHAIN_RE = re.compile(
    r"\bproj\.([A-Za-z_]\w*)((?:\s*\[[^\]\n]*\]|\s*\([^()\n]*\)|\.[A-Za-z_]\w*)*)"
)
CHAIN_TOKEN_RE = re.compile(r"\[[^\]\n]*\]|\([^()\n]*\)|\.[A-Za-z_]\w*")
BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
CAMEL_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*\b")
BINDING_RE = re.compile(r"\bproj\s*=\s*session\.(\w+_projection)\b")
PHASE_LINE_CODE = "proj.phase"
QUOTED_RE = re.compile(r'"([^"]+)"')

_MISSING = object()


# ---------------------------------------------------------------------------
# Fence-aware parsing (D2: a regex/line-based scanner, no markdown parser --
# verified against the actual corpus: 46 fence-delimiter lines across the 5
# mode docs, no nesting, no ~~~ fences, no info strings beyond "python").
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Fence:
    path: Path
    start_line: int
    info: str
    lines: list[tuple[int, str]]  # (1-based line number, raw text) strictly inside the fence


def parse_fences(path: Path) -> list[Fence]:
    text = path.read_text()
    out: list[Fence] = []
    open_line: int | None = None
    info = ""
    buf: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("```"):
            if open_line is None:
                open_line = lineno
                info = raw[3:].strip()
                buf = []
            else:
                out.append(Fence(path=path, start_line=open_line, info=info, lines=buf))
                open_line = None
        elif open_line is not None:
            buf.append((lineno, raw))
    if open_line is not None:
        raise AssertionError(f"{path}: unterminated fence starting at line {open_line}")
    return out


def non_fence_lines(path: Path) -> list[tuple[int, str]]:
    """Return (line_number, text) for every line NOT inside any fence."""
    text = path.read_text()
    lines = text.splitlines()
    in_fence = False
    result: list[tuple[int, str]] = []
    for lineno, raw in enumerate(lines, start=1):
        if raw.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            result.append((lineno, raw))
    return result


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment, respecting quoted strings (D6).

    A naive ``line.split("#")`` would misread a quote-heavy literal like
    ``quorum.md``'s ``details=b'{"rollout_plan": "gradual over 2 weeks"}'``.
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


# ---------------------------------------------------------------------------
# proj.* reference extraction (Check 1)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ProjRef:
    path: Path
    line: int
    raw: str
    head: str
    rest: str


def extract_proj_refs_from_fence(fence: Fence) -> list[ProjRef]:
    if fence.info != "python":
        # D2: unlabeled fences are the ASCII "Message flow" diagrams -- no
        # Python to scan, and scanning them would misread arrow glyphs etc.
        return []
    refs: list[ProjRef] = []
    for lineno, raw in fence.lines:
        code = _strip_comment(raw)
        for m in PROJ_CHAIN_RE.finditer(code):
            refs.append(
                ProjRef(
                    path=fence.path, line=lineno, raw=m.group(0), head=m.group(1), rest=m.group(2)
                )
            )
    return refs


def extract_proj_refs_from_prose(path: Path) -> list[ProjRef]:
    """D3: prose references are read from `` `...` `` backtick spans only --
    verified mechanically that stripping backticked spans from every
    non-fence line of all five docs leaves zero remaining ``proj.`` in
    prose, so no further heuristic is needed.
    """
    refs: list[ProjRef] = []
    for lineno, raw in non_fence_lines(path):
        for span in BACKTICK_SPAN_RE.findall(raw):
            for m in PROJ_CHAIN_RE.finditer(span):
                refs.append(
                    ProjRef(
                        path=path, line=lineno, raw=m.group(0), head=m.group(1), rest=m.group(2)
                    )
                )
    return refs


def extract_all_proj_refs(path: Path) -> list[ProjRef]:
    refs: list[ProjRef] = []
    for fence in parse_fences(path):
        refs.extend(extract_proj_refs_from_fence(fence))
    refs.extend(extract_proj_refs_from_prose(path))
    return refs


def _tokenize_rest(rest: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in CHAIN_TOKEN_RE.finditer(rest):
        text = m.group(0)
        if text.startswith("["):
            tokens.append(("SUBSCRIPT", text))
        elif text.startswith("("):
            tokens.append(("CALL", text))
        else:
            tokens.append(("ATTR", text))
    return tokens


def _camel_names(text: str) -> list[str]:
    return CAMEL_NAME_RE.findall(text)


def _dataclass_registry(*modules: ModuleType) -> dict[str, type]:
    registry: dict[str, type] = {}
    for mod in modules:
        for name, obj in vars(mod).items():
            if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                registry[name] = obj
    return registry


def _annassign_annotations(module: ModuleType) -> dict[str, str]:
    """Map ``self.<attr>`` -> its PEP-526 annotation source text.

    PEP 526 does not store attribute annotations anywhere at runtime, so
    this recovers them by AST-parsing the module for ``AnnAssign`` nodes
    targeting ``self.<attr>``.
    """
    source = inspect.getsource(module)
    tree = ast.parse(source)
    result: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        ):
            result[node.target.attr] = ast.unparse(node.annotation)
    return result


@dataclasses.dataclass
class ResolveResult:
    resolved: bool
    detail: str


def resolve_chain(
    *,
    instance: object,
    head: str,
    rest: str,
    dataclass_registry: dict[str, type],
    attr_annotations: dict[str, str],
) -> ResolveResult:
    cls = type(instance)
    if not hasattr(instance, head):
        return ResolveResult(False, f"head {head!r} not found on {cls.__name__}")

    tokens = _tokenize_rest(rest)
    if not tokens or tokens[-1][0] != "ATTR":
        # Either bare `proj.head`, or the chain's final suffix is a call/
        # subscript (`proj.requests.get(id)`) -- only the head is checked;
        # checking an intermediate `.get`/`.items`/etc. against the
        # container would be unsound (see module docstring / D4).
        return ResolveResult(True, "head only")

    tail = tokens[-1][1][1:]  # strip leading "."
    preceding = tokens[:-1]
    has_subscript_or_call = any(kind in ("SUBSCRIPT", "CALL") for kind, _ in preceding)

    if not has_subscript_or_call:
        # Direct chain (no intervening subscript/call): resolve on the live
        # object, hasattr(getattr(instance, head), tail), walking through
        # any preceding pure-dotted attrs first.
        obj: object = getattr(instance, head, _MISSING)
        for _kind, text in preceding:
            if obj is _MISSING:
                break
            obj = getattr(obj, text[1:], _MISSING)
        if obj is _MISSING:
            return ResolveResult(False, f"direct chain through {head!r} broke before {tail!r}")
        ok = hasattr(obj, tail)
        return ResolveResult(ok, f"direct traversal hasattr(..., {tail!r}) = {ok}")

    # Trailing attribute reached through a subscript/call: resolve against
    # the element/return record type, never the container (a drifted field
    # colliding with dict.get/.update/.values/.count/.copy/.pop must not
    # silently "resolve" against the container).
    first_kind, _ = preceding[0]
    if first_kind == "CALL":
        func = getattr(cls, head, None)
        ann = ""
        if func is not None and hasattr(func, "__annotations__"):
            ann = func.__annotations__.get("return", "") or ""
        source_desc = f"return annotation of {head}() = {ann!r}"
    else:
        ann = attr_annotations.get(head, "")
        source_desc = f"PEP-526 annotation of self.{head} = {ann!r}"

    candidates = _camel_names(ann)
    if not candidates:
        return ResolveResult(
            False, f"no candidate record type found via {source_desc} to check .{tail}"
        )

    searched = []
    for cand in candidates:
        dc = dataclass_registry.get(cand)
        if dc is None:
            searched.append(f"{cand} (not a known dataclass)")
            continue
        field_names = {f.name for f in dataclasses.fields(dc)}
        if tail in field_names:
            return ResolveResult(True, f"{tail!r} found on {cand}")
        searched.append(f"{cand} (fields: {sorted(field_names)})")

    return ResolveResult(
        False, f".{tail} not found on any candidate from {source_desc}; searched: {searched}"
    )


# ---------------------------------------------------------------------------
# D1: derive the mode -> projection-class mapping. No hardcoded table --
# every BaseSession subclass exposes exactly one `*_projection` property
# whose return annotation names its projection class.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ModeBinding:
    property_name: str
    projection_class: type
    projection_module: ModuleType


def discover_mode_bindings() -> list[ModeBinding]:
    bindings: list[ModeBinding] = []
    for _name, obj in vars(macp_sdk).items():
        if not (isinstance(obj, type) and issubclass(obj, BaseSession) and obj is not BaseSession):
            continue
        props = [(pname, p) for pname, p in vars(obj).items() if isinstance(p, property)]
        assert len(props) == 1, (
            f"{obj.__name__}: expected exactly one `*_projection` property, found {props}"
        )
        pname, prop = props[0]
        assert prop.fget is not None
        ann = prop.fget.__annotations__.get("return")
        assert isinstance(ann, str) and ann, f"{obj.__name__}.{pname}: missing a return annotation"
        session_module = sys.modules[obj.__module__]
        cls = vars(session_module).get(ann)
        assert cls is not None, (
            f"{obj.__name__}.{pname}: could not resolve return annotation {ann!r} "
            f"in {session_module.__name__}"
        )
        bindings.append(
            ModeBinding(
                property_name=pname,
                projection_class=cls,
                projection_module=sys.modules[cls.__module__],
            )
        )
    assert bindings, "discovered zero BaseSession subclasses -- is macp_sdk importable?"
    return bindings


ALL_BINDINGS = discover_mode_bindings()
BINDINGS_BY_PROPERTY = {b.property_name: b for b in ALL_BINDINGS}


def find_file_binding(path: Path) -> ModeBinding:
    text = path.read_text()
    names = set(BINDING_RE.findall(text))
    assert names, f"{path}: no `proj = session.<mode>_projection` binding found"
    assert len(names) == 1, f"{path}: multiple distinct projection bindings found: {sorted(names)}"
    (name,) = names
    binding = BINDINGS_BY_PROPERTY.get(name)
    assert binding is not None, f"{path}: unknown projection property {name!r}"
    return binding


def _mode_doc_paths() -> list[Path]:
    paths = sorted(DOCS_MODES_DIR.glob("*.md"))
    assert paths, f"no files found under {DOCS_MODES_DIR}"
    return paths


MODE_DOC_PATHS = _mode_doc_paths()
CHECK1_PATHS = MODE_DOC_PATHS + EXTRA_CHECK1_FILES


def _check2_modules() -> list[ModuleType]:
    """Scope to `docs/modes/*.md` only -- see Check 2's docstring below for why."""
    mods: dict[str, ModuleType] = {macp_sdk.__name__: macp_sdk}
    for b in ALL_BINDINGS:
        mods[b.projection_module.__name__] = b.projection_module
    for _name, obj in vars(macp_sdk).items():
        if isinstance(obj, type) and issubclass(obj, BaseSession) and obj is not BaseSession:
            session_module = sys.modules[obj.__module__]
            mods[session_module.__name__] = session_module
    mods[base_projection_module.__name__] = base_projection_module
    mods[core_pb2.__name__] = core_pb2
    mods[handoff_pb2.__name__] = handoff_pb2
    return list(mods.values())


CHECK2_MODULES = _check2_modules()


# ---------------------------------------------------------------------------
# Check 3: phase value-domain equality
# ---------------------------------------------------------------------------


def _doc_phase_domain(path: Path) -> tuple[int, set[str]] | None:
    """Find `proj.phase # "A" | "B" | ...`, joining continuation comment
    lines within the same fence (task.md/handoff.md wrap across 2 lines)."""
    for fence in parse_fences(path):
        if fence.info != "python":
            continue
        for i, (lineno, raw) in enumerate(fence.lines):
            code = _strip_comment(raw).strip()
            if code != PHASE_LINE_CODE:
                continue
            texts = [raw.split("#", 1)[1] if "#" in raw else ""]
            j = i + 1
            while j < len(fence.lines):
                _, nxt = fence.lines[j]
                stripped = nxt.strip()
                if stripped.startswith("#"):
                    texts.append(stripped[1:])
                    j += 1
                else:
                    break
            joined = " ".join(texts)
            return lineno, set(QUOTED_RE.findall(joined))
    return None


def _extract_phase_literals(module: ModuleType) -> set[str]:
    """AST-derive the real reachable `phase` string set: every
    `self.phase = "X"` literal and every `self._set_phase("X")` literal
    argument in *module*, plus `"Committed"` (set directly by
    `base_projection.py`, universal across all modes)."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    phases: set[str] = {"Committed"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "phase"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    phases.add(node.value.value)
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "_set_phase"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                phases.add(node.args[0].value)
    return phases


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", CHECK1_PATHS, ids=lambda p: p.name)
def test_proj_references_resolve(path: Path) -> None:
    """Check 1 (primary): every `proj.*` reference resolves against the real
    projection/record classes. Asserting a binding was found for every file
    in CHECK1_PATHS also closes D1's "new mode doc silently skipped" hole --
    find_file_binding() fails loudly rather than skipping a file whose fence
    uses a different variable name.
    """
    binding = find_file_binding(path)
    instance = binding.projection_class()
    registry = _dataclass_registry(binding.projection_module, base_projection_module)
    attr_ann = {
        **_annassign_annotations(base_projection_module),
        **_annassign_annotations(binding.projection_module),
    }

    refs = extract_all_proj_refs(path)
    failures = []
    for ref in refs:
        result = resolve_chain(
            instance=instance,
            head=ref.head,
            rest=ref.rest,
            dataclass_registry=registry,
            attr_annotations=attr_ann,
        )
        if not result.resolved:
            rel = ref.path.relative_to(REPO_ROOT)
            failures.append(
                f"{rel}:{ref.line}: `{ref.raw}` unresolved against "
                f"{binding.projection_class.__name__} -- {result.detail}"
            )
    assert not failures, "\n" + "\n".join(failures)


@pytest.mark.parametrize("path", MODE_DOC_PATHS, ids=lambda p: p.name)
def test_type_names_resolve(path: Path) -> None:
    """Check 2: every `*Record`/`*Projection`/`*Session`/`*Payload`/
    `*Anomaly` type name used anywhere -- including inside `#` comments,
    which Check 1 cannot see -- is a real symbol. This is the check that
    catches a correct head with a fictional type named only in a comment
    (#106's real `proj.terminal_rejections # list[TerminalRejectRecord]` was
    caught by its head being wrong too; a head-correct variant would not be
    caught by Check 1 at all).

    Scoped to `docs/modes/*.md` only, deliberately narrower than Check 1:
    this check has no `proj.` receiver to anchor on, so widening it to the
    two EXTRA_CHECK1_FILES would false-positive on docs/architecture.md's
    gRPC RPC-method table (`CancelSession`, `GetSession`, `StreamSession` --
    real RPC names, not SDK type names, and not part of this bug class).
    """
    text = path.read_text()
    names = sorted(set(CAMEL_TYPE_RE.findall(text)))
    failures = [
        f"{path.relative_to(REPO_ROOT)}: type name {name!r} not found in any of "
        f"{[m.__name__ for m in CHECK2_MODULES]}"
        for name in names
        if not any(name in vars(m) for m in CHECK2_MODULES)
    ]
    assert not failures, "\n" + "\n".join(failures)


@pytest.mark.parametrize("path", MODE_DOC_PATHS, ids=lambda p: p.name)
def test_phase_domain_matches(path: Path) -> None:
    """Check 3: a documented `proj.phase # "A" | "B" | ...` value domain
    matches the code's actual reachable set. Catches an *incomplete*
    enumeration, which Check 1 is structurally blind to (`phase` itself
    resolves fine as a head) -- pre-fix `handoff.md` listed five phases
    where six exist (missing "ContextSharing"), exactly this failure mode.
    """
    binding = find_file_binding(path)
    found = _doc_phase_domain(path)
    assert found is not None, f'{path}: no `proj.phase # "..."` line found in a python fence'
    lineno, documented = found
    real = _extract_phase_literals(binding.projection_module)
    assert documented == real, (
        f"{path}:{lineno}: documented phase domain {sorted(documented)} != "
        f"actual {sorted(real)} (derived from {binding.projection_module.__name__})"
    )


def test_resolver_self_test_catches_known_drift_shapes() -> None:
    """Check 4a (non-vacuity): prove the resolver actually rejects bad
    references and accepts good ones, using synthetic examples mirroring the
    historical defects this guard exists to catch. All four cases are
    checked against ProposalProjection; `offers` is absent there exactly as
    it was absent from the real #111 target (HandoffProjection) -- the case
    proves head-drift detection generically, not a replay of #111's exact
    class.
    """
    binding = BINDINGS_BY_PROPERTY["proposal_projection"]
    instance = binding.projection_class()
    registry = _dataclass_registry(binding.projection_module, base_projection_module)
    attr_ann = {
        **_annassign_annotations(base_projection_module),
        **_annassign_annotations(binding.projection_module),
    }

    cases: list[tuple[str, bool]] = [
        ("proj.definitely_not_a_real_attribute", False),  # head drift
        ('proj.proposals["p1"].disposition', False),  # #106 shape: head ok, field drift
        ('proj.offers["h1"].target_participant', False),  # #111 shape: head itself wrong
        ('proj.proposals["p1"].title', True),  # known-good
    ]
    for text, expect_resolved in cases:
        m = PROJ_CHAIN_RE.search(text)
        assert m is not None, f"self-test extraction regex failed to match {text!r}"
        result = resolve_chain(
            instance=instance,
            head=m.group(1),
            rest=m.group(2),
            dataclass_registry=registry,
            attr_annotations=attr_ann,
        )
        assert result.resolved is expect_resolved, (
            f"{text!r}: expected resolved={expect_resolved}, "
            f"got {result.resolved} ({result.detail})"
        )


def test_resolver_found_a_nonzero_number_of_references() -> None:
    """Check 4b (non-vacuity floor): a future refactor that breaks the
    extraction regex must fail loudly (zero matches) rather than silently
    pass over an empty corpus. Today's corpus yields well over 80.
    """
    total = sum(len(extract_all_proj_refs(p)) for p in CHECK1_PATHS)
    assert total >= 80, f"expected >= 80 proj.* references across the corpus, found {total}"
