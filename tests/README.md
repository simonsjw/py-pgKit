# py-pgKit Test Suite Documentation

This document describes the test framework for **py-pgKit** (`py_pgkit`), located in the `tests/` directory. It is intended for intermediate Python developers who want to:

- Understand what the tests cover
- Run and debug the existing tests
- Investigate failures or unexpected behavior
- Draft new tests following the established patterns

**Principle**: All tests are **unit tests that use heavy mocking**. No live PostgreSQL database is required (or used) during `pytest` runs. This keeps the suite fast, deterministic, portable, and suitable for CI environments without Docker or external services.

## Quick Start

```bash
# From the repository root
pytest -q --asyncio-mode=auto          # Recommended (config already sets asyncio_mode)
# or simply
pytest -q
```

- `-q` : quiet (dots + summary)
- All async tests are automatically handled thanks to `pytest-asyncio` + configuration in `pyproject.toml`.

### Useful pytest Flags for Investigation

| Flag                                           | Purpose                                                                | Example                                                              |
|------------------------------------------------|------------------------------------------------------------------------|----------------------------------------------------------------------|
| `-v`                                           | Verbose output (shows test names + docstrings)                         | `pytest -v tests/test_query.py`                                      |
| `-s` / `--capture=no`                          | Show `print()` statements and captured stdout (great for DEBUG prints) | `pytest -s tests/test_load.py::test_bulk_insert_dicts_default_chunk` |
| `--pdb`                                        | Drop into interactive debugger on first failure                        | `pytest --pdb tests/test_query.py`                                   |
| `-k "expression"`                              | Run tests matching substring / keyword (e.g. "bulk" or "error")        | `pytest -k "bulk_insert and error"`                                  |
| `--lf` / `--last-failed`                       | Re-run only the tests that failed in the previous run                  | `pytest --lf`                                                        |
| `--tb=short` / `long`                          | Control traceback length (short is usually enough)                     | `pytest --tb=short -q`                                               |
| `-q --asyncio-mode=auto`                       | Explicitly force asyncio mode (already in config)                      | As shown in your command                                             |
| `--cov=src/py_pgkit --cov-report=term-missing` | Code coverage (requires `pytest-cov`)                                  | `pip install pytest-cov && pytest --cov=...`                         |

**Pro tip**: Add temporary `print("DEBUG:", mock_conn.execute.call_args)` inside a test (as already done in one place in `test_db_tools.py`) and run with `-s` to inspect exactly what SQL / arguments are being passed to the mock.

## Test Configuration (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
asyncio_default_fixture_loop_scope = "function"
```

- `asyncio_mode = "auto"`: Every `async def` test is automatically wrapped; no need to mark every test (though `@pytest.mark.asyncio` is still used explicitly for clarity).
- `asyncio_default_fixture_loop_scope = "function"`: Each test gets its **own fresh event loop**. This prevents cross-test pollution (especially important when mocking `asyncio.get_event_loop` or pool objects).
- No `pytest.ini` file — configuration lives in `pyproject.toml` (modern best practice).

## Shared Fixtures (in `conftest.py`)

These are the building blocks that make the test suite clean and powerful:

### `settings` (function scope)
Minimal valid `PgSettings` pointing to a fake `testdb`. Never actually connects.

```python
return PgSettings(
    host="localhost", port=5432, database="testdb",
    user="testuser", password="testpass", ...
)
```

### `mock_pool_conn`
Returns `(mock_pool, mock_conn)` where `mock_conn` is an `AsyncMock` with the common methods (`fetch`, `execute`, `copy_records_to_table`, `fetchval`) and `mock_pool.acquire()` returns a proper async context manager. Useful for simpler tests.

### `patch_get_pool` (most important fixture)
**The workhorse for all database-interacting tests.**

- Patches `get_pool` (and the same function imported in `db.methods.db_tools`, `load`, `query`) to return a controlled mock pool.
- The inner `mock_conn` has realistic `AsyncMock` behavior.
- Automatically started/stopped around the test.
- **Why multiple patches?** Different modules import `get_pool` differently; this ensures the mock is seen everywhere.

Usage pattern in almost every DB test:
```python
@pytest.mark.asyncio
async def test_something(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"id": 1}]
    # ... call the function under test ...
    mock_conn.fetch.assert_awaited_once()
    # Inspect the generated SQL
    sql = mock_conn.fetch.call_args[0][0]
    assert "SELECT" in sql and "WHERE" in sql
```

### Data Fixtures
- `sample_records_dict` / `sample_records_tuples`: Realistic row data for `bulk_insert` tests.
- `multipart_sql_script`: A realistic multi-statement script containing comments (`--` and `/* */`), `CREATE`, `INSERT ... RETURNING`, `SELECT`, `UPDATE` — perfect for exercising the SQL script parser.

### `clear_pool_registry` (autouse in `test_pool.py`)
Resets the internal `_POOL_REGISTRY` dict before/after every pool test to guarantee isolation.

## Test File Overview

| File                   | Focus Area                                                                                             | Key Techniques Used                                                                                                 |
|------------------------|--------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| `test_settings.py`     | `PgSettings` Pydantic model (validation, env vars, frozen)                                             | `monkeypatch.setenv`, `pytest.raises(ValidationError)`                                                              |
| `test_pool.py`         | Connection pool caching, key generation, cleanup                                                       | `patch("...asyncpg.create_pool")`, autouse registry clearer, `assert p1 is p2` (caching)                            |
| `test_logging.py`      | `getLogger`, `DBLogHandler`, `configure_logging`                                                       | `patch("...asyncio.get_event_loop")`, handler inspection, idempotency checks                                        |
| `test_load.py`         | `load_*_to_memory`, `bulk_insert` (chunking is the tricky part)                                        | `patch_get_pool`, explicit `batch_size` tests, dict vs tuple handling, error propagation                            |
| `test_query.py`        | `execute_query`, `run_multi_statement_sql_script` (comment stripping, SELECT heuristic, stop_on_error) | Complex `side_effect` lists for mixed fetch/execute, `stop_on_error=True/False` paths, malformed comment edge cases |
| `test_db_tools.py`     | `ensure_functions_loaded`, `ensure_partition_exists`                                                   | DuplicateTableError swallowing, list vs string input                                                                |
| `test_builder.py`      | `DatabaseBuilder` lifecycle and partition logic                                                        | `capsys` + `caplog` for logging assertions, flag-based step verification                                            |
| `test_imports.py`      | Package import structure & re-exports                                                                  | Layered import checks (internal → public API)                                                                       |
| `test_import_by_fn.py` | Public API surface verification                                                                        | `hasattr` + callable checks at every import level                                                                   |

## How Database Writes & Side-Effects Are Managed (No Real DB!)

**Core strategy**: Never let production code touch a real connection pool or execute real SQL during tests.

1. **Mock at the `get_pool` boundary** — the single source of truth for all DB access.
2. **Control return values and side effects**:
   - `mock_conn.fetch.return_value = [...]`
   - `mock_conn.execute.side_effect = ["OK", Exception("boom"), "OK3"]`
   - `mock_conn.copy_records_to_table.return_value = None`
3. **Assert on the generated SQL and call patterns** (this is how you verify correctness without a DB):
   ```python
   sql = mock_conn.fetch.call_args[0][0]
   assert "SELECT * FROM users" in sql
   assert "WHERE active = $1" in sql
   assert mock_conn.copy_records_to_table.call_count == 2  # chunking worked
   ```
4. **Chunking / batch_size testing** (`bulk_insert`):
   - Small `batch_size=2` → forces multiple `copy_records_to_table` calls
   - Large `batch_size=10000` → single call even with 30 rows
5. **Error paths are first-class**:
   - `test_bulk_insert_error_propagates`
   - `test_run_multi_statement_stop_on_error` (stops early, records error message)
   - `test_ensure_partition_exists_duplicate_ignored` (swallows `DuplicateTableError`)

This approach means you can safely test INSERT/UPDATE/CREATE logic, SQL generation, and error handling without ever risking your development database.

## Async Testing Patterns

- All DB functions are `async` → tests are `async def` + `@pytest.mark.asyncio`
- Fresh event loop per test (via `asyncio_default_fixture_loop_scope = "function"`)
- When the code under test internally calls `asyncio.get_event_loop()` (e.g. `DBLogHandler.emit`), we patch it:
  ```python
  with patch("py_pgkit.logging.core.asyncio.get_event_loop") as mock_loop:
      mock_loop.return_value = AsyncMock()
      handler.emit(record)
  ```
- Proper async context managers are mocked for `pool.acquire()`:
  ```python
  mock_acquire_cm = AsyncMock()
  mock_acquire_cm.__aenter__.return_value = mock_conn
  mock_pool.acquire.return_value = mock_acquire_cm
  ```

## Logging & Output Capture

- `caplog` fixture used in `test_builder.py` to assert warning messages.
- `capsys` also used in the same test.
- DB logging handler tests verify that `DBLogHandler` is attached exactly once (idempotent) and that `emit()` never raises (best-effort async scheduling).

## How to Investigate a Failing Test

1. Run with `-v -s --tb=long` to see exactly which assertion failed and any debug prints.
2. Add a temporary `print("DEBUG call_args:", mock_xxx.call_args)` right before the failing assertion.
3. Use `--pdb` to inspect live objects at the failure point.
4. For async-specific weirdness: check that the test has `@pytest.mark.asyncio` and that you're not mixing sync/async fixtures incorrectly.
5. For import errors: run `test_imports.py` and `test_import_by_fn.py` in isolation — they are excellent canaries for broken `__init__.py` re-exports.

## How to Draft a New Test (Recommended Pattern)

```python
import pytest
from py_pgkit.db.methods.your_module import your_function

@pytest.mark.asyncio
async def test_your_function_happy_path(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.fetch.return_value = [{"result": 42}]

    result = await your_function("some_arg", settings)

    mock_conn.fetch.assert_awaited_once()
    sql = mock_conn.fetch.call_args[0][0]
    assert "EXPECTED SQL FRAGMENT" in sql
    assert result == [{"result": 42}]

@pytest.mark.asyncio
async def test_your_function_error_path(settings, patch_get_pool):
    mock_pool, mock_conn = patch_get_pool
    mock_conn.execute.side_effect = Exception("boom")

    with pytest.raises(Exception, match="boom"):
        await your_function(..., settings)
```

**Tips for new tests**:
- Always use the `settings` + `patch_get_pool` combo for anything that calls into the DB layer.
- Test both success **and** error paths.
- For complex SQL generation, assert on substrings of the generated query.
- Add edge-case fixtures to `conftest.py` if you need reusable test data.
- Keep tests focused — one behavior per test function.
- Update `test_imports.py` / `test_import_by_fn.py` if you add new public functions.

## Current Gaps & Future Ideas (for contributors)

- No integration tests against a real PostgreSQL instance (intentionally).
- No performance / load tests for `bulk_insert` chunking.
- `run_multi_statement_sql_script` comment stripping is heuristic-based — more adversarial test cases could be added.
- Consider adding `pytest-docker` or `testcontainers` for optional real-DB smoke tests in the future.

## Summary

The test suite is deliberately **mock-heavy and isolation-focused**. This design lets any developer:

- Run the entire suite in < 2 seconds on a laptop with no Docker/Postgres
- Confidently refactor internal DB code without fear of breaking production data
- Quickly understand exactly what SQL is being generated by inspecting `call_args`

By following the patterns above (especially `patch_get_pool` + strict mock assertions), you can add new functionality and tests that will keep the suite maintainable.

Happy testing! If you find a tricky edge case that the current mocks don't cover well, feel free to improve the fixtures in `conftest.py` — that's exactly how the suite evolved.
