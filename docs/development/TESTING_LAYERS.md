# Test layers

The repository contains both maintained product tests and historical diagnostic
scripts inherited from earlier TradingAgents-CN releases. They intentionally do
not share one execution policy.

## Default maintained suite

```bash
.venv/bin/python -m pytest
```

This runs deterministic unit and contract tests without requiring real market
APIs, credentials, MongoDB, Redis, or an LLM.

## Integration suite

```bash
.venv/bin/python -m pytest -m integration
```

Integration tests may require Docker services. Individual test modules should
still mock paid LLM and market APIs unless the test explicitly documents live
credentials.

## Historical and live diagnostics

```bash
.venv/bin/python -m pytest tests/path/to/test_file.py -m live
```

These scripts are opt-in. Some reference APIs removed by later architecture
changes and must be migrated before they can return to the maintained suite.
The explicit `collect_ignore` list in `tests/conftest.py` records scripts that
cannot currently be imported safely.
