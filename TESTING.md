# Testing

TokenCost uses [pytest](https://docs.pytest.org/). Tests live in `tests/` and run
against the repo `venv`. They never touch the live `tracker.db` — DB-backed tests
use a temp database via the `tmp_db` / `seed_requests` fixtures in `conftest.py`.

## Run locally (non-CI)

First time (installs `pytest` + `respx` into the venv):

```bash
./run-tests.sh --install
```

After that:

```bash
./run-tests.sh                 # full suite
./run-tests.sh -k calc_cost    # filter by name (forwarded to pytest)
./run-tests.sh -v              # verbose
```

Equivalent without the wrapper:

```bash
./venv/bin/python -m pip install -r requirements-dev.txt   # once
./venv/bin/python -m pytest tests/ -v
```

## Layout

| File | Covers |
|------|--------|
| `tests/test_cost_accounting.py`     | `db.calc_cost` multipliers, `proxy._parse_anthropic` 1h-split, `db._pause_analysis` observed-TTL |
| `tests/test_request_passthrough.py` | routing normalization (`proxy._normalize_for_downgrade`), cache injection, thinking passthrough (docs/adr/0001) |
| `tests/test_proxy.py`               | proxy pure helpers + `proxy_anthropic` integration (TestClient + respx-mocked upstream) |
| `tests/test_optimizer.py`           | optimizer routing/cache/dedup pure functions |
| `tests/test_import_history.py`      | importer dedup (`msg_uuid`), cutoff double-counting guard, Claude JSONL parser |
| `tests/test_db_stats.py`            | numeric aggregations (`get_stats`, cost/cache/savings rollups) |
| `tests/test_projects.py`            | `projects.get_project_stats` aggregation |

## Conventions

- **No live DB.** Use the `tmp_db` fixture (fresh temp SQLite) or `seed_requests`
  (insert rows with explicit timestamps) from `conftest.py`.
- **No network.** Proxy integration tests mock the Anthropic upstream with
  `respx`; nothing hits the real API.
- **No clock dependence.** Aggregation tests query with `period="all"` and
  fixed-timestamp seeds, so they don't depend on the wall clock.

## CI

`.github/workflows/tests.yml` runs the same suite on every push and pull request.
