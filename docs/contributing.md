# Contributing

Internal notes for maintainers of `macp-sdk-python`. End-user docs live under `docs/`.

## Local setup

```bash
make setup            # pip install -e ".[dev,docs]"
```

Runs the full dev toolchain: `ruff`, `mypy`, `pytest`, `build`, `twine`, and the mkdocs site.

## Green-bar gates

Every PR must pass the shared quality gate in `.github/workflows/checks.yml`
(reused by both `ci.yml` and `publish.yml` so the two can't drift):

```bash
make lint                # ruff check + ruff format --check
make typecheck           # mypy src/macp_sdk/
make test                # unit tests + coverage gate (fails under 85%)
make test-conformance    # fixture replay — runs on every PR in CI too
```

Or run the whole thing in one command: `make test-all` (also runs
`make test-integration`, which self-skips without a local runtime).

A local green run proves less than CI does, though: `checks.yml` runs the
unit tests across a 3.11 / 3.12 / 3.13 matrix, while `make test` runs
whichever single interpreter you have active — a local pass is necessary
but not sufficient.

The fixture drift gate (`make verify-fixtures`) is a separate, related gate —
see below for what it checks and which workflow runs it.

The coverage gate (85%, **branch** coverage) is configured once in
`pyproject.toml` under `[tool.coverage.*]`; `make test`, `make coverage`, and
CI all invoke pytest with a bare `--cov` and inherit it — don't re-specify
thresholds on the command line. Pytest also runs with `--strict-markers` and
`filterwarnings = error`, so an undeclared marker or a new warning fails the
suite.

Integration tests require a running MACP runtime (see `CLAUDE.md`). They are
**not** run in CI — they stay a local gate. `tests/integration/conftest.py`
probes the target (`MACP_RUNTIME_TARGET`, default `127.0.0.1:50051`) and
auto-skips the whole directory when no runtime is reachable, so a bare
`pytest tests/` is always safe to run.

`examples/*.py` are executed, not just compiled — `tests/integration/test_examples_run.py`
runs each one as a subprocess against a live runtime and asserts exit 0
(`tests/unit/test_examples_smoke.py` only checks that they parse). Run
`make test-integration` before touching anything under `examples/`. A new
example file must be classified in that test's `RUN` or `EXCLUDED` list —
its `test_coverage_parity` case fails otherwise.

The fixture drift gate (`make verify-fixtures`) diffs this repo's vendored
conformance fixtures against the canonical copies in the spec repo, in both
directions — a canonical file missing or differing locally fails as `DRIFT`,
a local-only file fails as `EXTRA`. It checks every pair listed in the
`Makefile`'s `FIXTURE_DIR_PAIRS`, which covers both `tests/conformance/` and
`tests/vectors/cmt-hash/`. `make sync-fixtures` refreshes all of them from
the spec repo checkout pointed at by `SPEC_CONFORMANCE_DIR`; sync copies but
never deletes, so a file flagged `EXTRA` must be removed by hand. This gate
runs in its own workflow, `conformance-fixtures.yml`, on every push and PR
targeting `main` — it is not part of the `checks.yml` gate above.

Both targets need the spec repo on disk. `SPEC_CONFORMANCE_DIR` defaults to
`../multiagentcoordinationprotocol/schemas/conformance`, i.e. a sibling clone
next to this one; without it `make verify-fixtures` fails immediately with
`spec repo not found`. Clone the spec repo alongside this one, or override the
variable: `make verify-fixtures SPEC_CONFORMANCE_DIR=/path/to/schemas/conformance`.

## Bumping `macp-proto`

The SDK pins `macp-proto` with a **tight upper bound** (currently `>=0.1.6,<0.1.9`). This is intentional: proto changes can silently break envelope serialization, projection parsing, or RPC signatures, and we want every new minor to pass the conformance suite before users see it.

> **0.1.6 note (SDK 0.5.0):** the floor is `0.1.6` because the SDK uses the runtime v0.5.0 wire surface — `SessionStartPayload.max_suspend_ms` (0.1.5), `HandoffAcceptPayload.implicit` and `ListSessionsRequest.page_size`/`page_token` (0.1.6), and the canonical `macp.modes.multi_round.v1.ContributePayload` encoding (0.1.4). **Critically, macp-proto 0.1.6's gencode was produced by protobuf 7.35.0 / grpc 1.82.0** and protobuf enforces this at *import time* (`runtime_version.VersionError` under an older protobuf), so the floors moved together: **`protobuf>=7.35.0`** and **`grpcio>=1.82.0`**. macp-proto's own METADATA floors are looser than the gencode requires, so do not trust them — derive the floors from the gencode. There is no way to straddle protobuf majors; consumers pinned to protobuf 6.x cannot upgrade to SDK 0.5.0.
>
> **grpcio 1.82.0 yank workaround:** grpcio 1.82.0 stable was yanked from PyPI (metadata-only error — bad protobuf range, [grpc/grpc#42906](https://github.com/grpc/grpc/issues/42906)) with no newer stable published, which made a `grpcio>=1.82.0` floor unsatisfiable in fresh installs. The SDK's floor is therefore temporarily **`grpcio>=1.82.0rc2`** (the rc passes the gencode's `GRPC_GENERATED_VERSION` check), and `macp-proto` is capped **`<0.1.8`** because 0.1.8's METADATA re-declares the unsatisfiable stable floor. Re-widen both once a stable grpcio ≥ 1.82.1 ships. `grpcio-tools` was dropped from the dev extras entirely — this repo generates no protos, and every installable grpcio-tools caps `protobuf<7`.
>
> **0.1.3 note (historical):** 0.1.3 introduced the suspend/cancel/supersede surface and required `grpcio>=1.81.1`.

To move the pin:

1. Check the `macp-proto` release notes for the target version.
2. Install the new version in a dev venv: `pip install 'macp-proto==X.Y.Z'`.
3. Run the full test matrix, including integration and conformance:
   ```bash
   make test-all
   make test-conformance
   make test-integration   # against a matching-version runtime
   ```
4. If anything breaks, fix the SDK (or report the proto regression upstream).
5. Once green, update the pin in `pyproject.toml` (`macp-proto>=…,<…`) and this doc.
6. Describe the upgrade in the commit body (`chore(deps): …` or `fix: …` if
   it changes behavior) — release-please copies it into `CHANGELOG.md` when
   the release PR is cut. Don't hand-edit `CHANGELOG.md` (see Release
   process below).

The CI job `proto-drift` (see `.github/workflows/proto-drift.yml`, Q-16) runs the conformance suite against `macp-proto>=0.1.0` resolved from PyPI daily and opens an issue if it breaks — don't wait for that to notice a problem, but treat its failure as an action item.

## Release process

**Releases are automated by `release-please` — do not bump the version or
push a tag by hand.**

1. Merge conventional-commit PRs to `main` as usual (`fix:`, `feat:`,
   `feat!:`/`fix!:` for breaking changes, `docs:`, `chore:`, `ci:`, `test:`,
   …). **Write a good commit body, not just a one-line subject** —
   release-please copies the commit body straight into `CHANGELOG.md`, so
   the body is the changelog entry. This is the *only* place changelog prose
   comes from; there is no separate step to write one.
2. `release-please.yml` (`.github/workflows/release-please.yml`) runs on
   every push to `main`. It opens or updates a standing **release PR** that
   bumps `pyproject.toml` + `.release-please-manifest.json` and rewrites
   `CHANGELOG.md` from the conventional-commit history since the last
   release. `bump-minor-pre-major: true` in `release-please-config.json`
   means a `feat!`/`fix!` bumps the minor version pre-1.0 (this is why
   0.7.0 → 0.8.0 was a `feat!`, not a major bump).
3. Merging the release PR bumps the version, tags `vX.Y.Z`, and creates a
   GitHub Release — all in one merge. Do this when the accumulated changes
   are ready to ship; there is no separate "cut a release" step.
4. The Release event (created with a GitHub App token, not `GITHUB_TOKEN`,
   which is why it actually fires `publish.yml`) triggers `publish.yml`. It
   re-runs the full `checks.yml` gate (lint, typecheck, unit matrix,
   conformance), verifies the tag matches the `pyproject.toml` version,
   builds, `twine check`s, and uploads to PyPI via trusted publishing
   (OIDC) — no token or manual `twine upload` involved. The `publish` job
   must stay in `publish.yml` under the `pypi` environment — the PyPI
   trusted-publisher config pins the repo, the workflow filename, and the
   environment name; renaming any of the three silently breaks publishing.
5. PyPI publishes are immutable, but `skip-existing: true` makes re-running
   `publish.yml` for an already-published version a safe no-op.

**Never hand-edit `CHANGELOG.md`.** release-please owns the top of the
file — it inserts each new `## [X.Y.Z]` section there from the commit
history since the last release. A hand-written section left at the top
(e.g. an `## Unreleased` block) does not get consumed or merged by
release-please; it just sits above every future generated section,
permanently mislabeling already-released work as unreleased. If a change
needs prose richer than a one-line commit subject, put that prose in the
commit body — that is what release-please will carry into the changelog.

## CI layout

- `checks.yml` — reusable (`workflow_call`) lint / typecheck / unit matrix
  (3.11–3.13) / conformance gate; called by both `ci.yml` and `publish.yml`.
- `ci.yml` — checks + build (`twine check`) on push/PR to `main`; cancels
  superseded runs, supports `workflow_dispatch`.
- `conformance-fixtures.yml` — zero-drift gate for vendored fixtures vs the
  spec repo; runs `make verify-fixtures` against the spec checkout, then the
  spec repo's own `lint_fixtures.py` for internal consistency.
- `proto-drift.yml` — daily canary against the latest published `macp-proto`.
- All actions are SHA-pinned with version comments; `.github/dependabot.yml`
  keeps the pins and the pip dev toolchain current (it deliberately ignores
  `macp-proto` — bumping that is manual absorption work, see above).

## Testing against an unreleased proto

```bash
make dev-link-protos   # installs ../multiagentcoordinationprotocol/packages/proto-python in editable mode
```

This is a pre-release workflow only; releases must pin a published `macp-proto` version.
