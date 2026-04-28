"""
Tests for py_pgkit.db.pool (get_pool, close_all_pools, caching)

Uses heavy mocking because real pool creation requires a live DB.
Focuses on caching logic, key generation, and cleanup.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import asyncpg

from py_pgkit.db.pool import get_pool, close_all_pools, _settings_key, _POOL_REGISTRY
from py_pgkit.db.settings import PgSettings


@pytest.fixture(autouse=True)
def clear_pool_registry():
    """Ensure clean pool registry between tests."""
    _POOL_REGISTRY.clear()
    yield
    _POOL_REGISTRY.clear()


def test_settings_key_consistency(settings):
    key1 = _settings_key(settings)
    key2 = _settings_key(settings)
    assert key1 == key2
    assert len(key1) == 16  # sha256 hexdigest[:16]


def test_settings_key_ignores_pool_and_echo(settings):
    s1 = settings
    # Exclude pool/echo fields to avoid duplicate keyword argument error
    data = s1.model_dump(exclude={"pool_min_size", "pool_max_size", "echo"})
    s2 = PgSettings(
        **data,
        pool_min_size=10,
        pool_max_size=50,
        echo=True,
    )
    assert _settings_key(s1) == _settings_key(s2)


@pytest.mark.asyncio
async def test_get_pool_creates_and_caches(settings):
    with patch("py_pgkit.db.pool.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
        fake_pool = AsyncMock(spec=asyncpg.Pool)
        mock_create.return_value = fake_pool

        p1 = await get_pool(settings)
        p2 = await get_pool(settings)

        assert p1 is p2  # cached
        mock_create.assert_called_once()  # only created once


@pytest.mark.asyncio
async def test_get_pool_different_settings_different_pools(settings):
    s2 = PgSettings(
        host="otherhost",
        database="otherdb",
        user="otheruser",
        password=None,
    )

    with patch("py_pgkit.db.pool.asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
        fake1 = AsyncMock()
        fake2 = AsyncMock()
        mock_create.side_effect = [fake1, fake2]

        p1 = await get_pool(settings)
        p2 = await get_pool(s2)

        assert p1 is not p2
        assert mock_create.call_count == 2


@pytest.mark.asyncio
async def test_close_all_pools_clears_registry(settings):
    with patch("py_pgkit.db.pool.asyncpg.create_pool") as mock_create:
        fake_pool = AsyncMock()
        mock_create.return_value = fake_pool

        await get_pool(settings)
        assert len(_POOL_REGISTRY) == 1

        await close_all_pools()
        assert len(_POOL_REGISTRY) == 0
        fake_pool.close.assert_awaited_once()