# Package bootstrap and offline checks (issue 03)

## Implemented

- `src/uzumaki` package (stdlib-only runtime, `py.typed`), Python 3.12.12 (`.python-version`).
- `uzumaki.providers.RecordedProvider.from_path(path)` and `provider.request(dict) -> dict`. It replays a versioned JSON file:
  `{"schema_version":"1.0","records":[{"request":{...},"result":{...}}]}`. Matching is exact on the request object (key order ignored). Each call returns an independent copy.
- Missing, unreadable or malformed configuration, unsupported `schema_version`, duplicate requests, and unknown or non-serializable requests raise `ValueError` with a fixed safe message. Messages never include paths, configuration content or request input.
- Deterministic fixture: `tests/fixtures/recorded_provider.json`. Tests block sockets, so no network is used.
- `requirements-dev.txt` pins all direct and transitive development tools; the build backend is pinned to `setuptools==78.1.0` and `wheel==0.45.1`.
- CI (`.github/workflows/ci.yml`): `contents: read`, checkout and setup-python pinned by commit SHA, Python 3.12.12, runs `make check`.

## Reproduce

Setup (network needed once): `python -m pip install -r requirements-dev.txt && python -m pip install --no-deps -e .`

Offline check: `make check` runs `ruff check`, `ruff format --check`, `mypy src`, `pytest` and `python -m build --no-isolation`. It installs nothing and needs no network or API key.

## Limitations

No runtime execution, replay of runs, sandboxing, live model support or network provider exists. The provider is a fixture replayer only. Contracts from issue 02 are not yet enforced by code. Results of `make check` are produced by CI or a separate validation run, not asserted here.
