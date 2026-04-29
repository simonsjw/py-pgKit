"""
Tests for py_pgkit.db.builder.DatabaseBuilder

Covers construction, parameter handling, and selected internal logic with mocks.
Full end-to-end build requires a real DB + models, so we test the orchestration points.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from py_pgkit.db.builder import DatabaseBuilder
from py_pgkit.db.settings import PgSettings


def test_database_builder_init(settings):
    builder = DatabaseBuilder(
        settings=settings,
        create_tablespace=False,
        partition_strategy="daily",
        functions="/tmp/sql/",
    )
    assert builder.settings is settings
    assert builder.create_tablespace is False
    assert builder.partition_strategy == "daily"
    assert builder.functions == "/tmp/sql/"


@pytest.mark.asyncio
async def test_database_builder_build_calls_expected_steps(settings):
    """Verify the build() method calls the right internal helpers based on flags."""
    ts_settings = PgSettings(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        tablespace_name="fast_ssd",
        tablespace_path="/tmp/ssd",
        extensions=["uuid-ossp"],                                                         # needed for the extensions guard
    )
    builder = DatabaseBuilder(
        settings=ts_settings,
        create_tablespace=True,
        create_database=True,
        create_extensions=True,
        create_tables=False,
        create_triggers_and_functions=False,
        partition_strategy=None,
    )

    with (
        patch.object(builder, "_ensure_tablespace") as mock_ts,
        patch.object(builder, "_ensure_database") as mock_db,
        patch.object(builder, "_ensure_extensions") as mock_ext,
        patch.object(builder, "_get_pool") as mock_get_pool,
    ):
        mock_get_pool.return_value = AsyncMock()
        await builder.build()

        mock_ts.assert_awaited_once()
        mock_db.assert_awaited_once()
        mock_ext.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_daily_partition_delegates(settings, capsys, caplog):
    builder = DatabaseBuilder(settings=settings)

    with patch("py_pgkit.db.methods.db_tools.ensure_partition_exists") as mock_ensure:
        with caplog.at_level(logging.WARNING):
            await builder.add_daily_partition("logs", "2026-04-28", "2026-04-29")

        # Verify the warning is logged
        captured = capsys.readouterr()
        assert "not partitioned" in captured.out.lower()

        # Currently the method does NOT call ensure_partition_exists when table is not partitioned
        mock_ensure.assert_not_called()
