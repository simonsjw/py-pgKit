"""py-pgkit test suite.

Run with:
    pytest -q --asyncio-mode=auto

All tests use mocks and require no live PostgreSQL instance.
For real integration testing, set DB_* environment variables and
uncomment / extend the integration markers.
"""
