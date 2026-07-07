# Contributing

Internal notes for maintainers of `macp-sdk-python`. End-user docs live under `docs/`.

## Local setup

```bash
make setup            # pip install -e ".[dev,docs]"
```

Runs the full dev toolchain: `ruff`, `mypy`, `pytest`, `build`, and the mkdocs site.

## Green-bar gates

Every PR must pass:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/macp_sdk/
pytest tests/unit tests/conformance -q
```

Integration and runtime smoke tests require a running MACP runtime — see `CLAUDE.md`.

## Bumping `macp-proto`

The SDK pins `macp-proto` with a **tight upper bound** (currently `>=0.1.6,<0.2.0`). This is intentional: proto changes can silently break envelope serialization, projection parsing, or RPC signatures, and we want every new minor to pass the conformance suite before users see it.

> **0.1.6 note (SDK 0.5.0):** the floor is `0.1.6` because the SDK uses the runtime v0.5.0 wire surface — `SessionStartPayload.max_suspend_ms` (0.1.5), `HandoffAcceptPayload.implicit` and `ListSessionsRequest.page_size`/`page_token` (0.1.6), and the canonical `macp.modes.multi_round.v1.ContributePayload` encoding (0.1.4). **Critically, macp-proto 0.1.6's gencode was produced by protobuf 7.35.0 / grpc 1.82.0** and protobuf enforces this at *import time* (`runtime_version.VersionError` under an older protobuf), so the floors moved together: **`protobuf>=7.35.0`** and **`grpcio>=1.82.0`**. macp-proto's own METADATA floors are looser than the gencode requires, so do not trust them — derive the floors from the gencode. There is no way to straddle protobuf majors; consumers pinned to protobuf 6.x cannot upgrade to SDK 0.5.0.
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
6. Note the upgrade in `CHANGELOG.md`.

The CI job `proto-drift` (see `.github/workflows/proto-drift.yml`, Q-16) runs the conformance suite against `macp-proto>=0.1.0` resolved from PyPI daily and opens an issue if it breaks — don't wait for that to notice a problem, but treat its failure as an action item.

## Release process

1. Bump `version` in `pyproject.toml` (and the `client_version` default in `src/macp_sdk/client.py` if it's a minor).
2. Append a dated entry to `CHANGELOG.md` describing the change.
3. `git commit` the bump + changelog on `main`.
4. `git tag vX.Y.Z` and `git push --tags`.
5. The `publish.yml` workflow runs the full test matrix, builds, and uploads to PyPI via trusted publishing.
6. PyPI publishes are immutable — double-check the tag before pushing.

## Testing against an unreleased proto

```bash
make dev-link-protos   # installs ../multiagentcoordinationprotocol/packages/proto-python in editable mode
```

This is a pre-release workflow only; releases must pin a published `macp-proto` version.
