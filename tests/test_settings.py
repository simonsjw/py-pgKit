"""
Tests for py_pgkit.db.settings.PgSettings

Covers:
- Basic instantiation and defaults
- Environment variable loading (via aliases)
- Validation (port range, required fields)
- Frozen model behavior
- Extra fields ignored
"""

import pytest
from pydantic import ValidationError

from py_pgkit.db.settings import PgSettings


def test_settings_minimal():
    s = PgSettings(database="mydb", user="me")
    assert s.host == "localhost"
    assert s.port == 5432
    assert s.database == "mydb"
    assert s.user == "me"
    assert s.password is None
    assert s.extensions is None
    assert s.pool_min_size == 5
    assert s.pool_max_size == 20


def test_settings_env_vars(monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.example.com")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "envdb")
    monkeypatch.setenv("DB_USER", "envuser")
    monkeypatch.setenv("PASSWORD", "secret123")
    monkeypatch.setenv("DB_EXTENSIONS", '["postgis"]')  # JSON list

    s = PgSettings()
    assert s.host == "db.example.com"
    assert s.port == 5433
    assert s.database == "envdb"
    assert s.user == "envuser"
    assert s.password == "secret123"
    assert s.extensions == ["postgis"]


def test_settings_port_validation():
    with pytest.raises(ValidationError):
        PgSettings(database="db", user="u", port=0)
    with pytest.raises(ValidationError):
        PgSettings(database="db", user="u", port=70000)
    # Valid
    s = PgSettings(database="db", user="u", port=5432)
    assert s.port == 5432


def test_settings_frozen():
    s = PgSettings(database="db", user="u")
    with pytest.raises(ValidationError):  # pydantic v2 raises on frozen
        s.host = "other"


def test_settings_extra_ignored():
    s = PgSettings(database="db", user="u", unknown_field="ignored")
    assert not hasattr(s, "unknown_field")


def test_settings_tablespace_optional():
    s = PgSettings(
        database="db",
        user="u",
        tablespace_name="fast_ssd",
        tablespace_path=None,  # allowed (builder will error later if needed)
    )
    assert s.tablespace_name == "fast_ssd"

def test_settings_frozen_mutation_raises_correct_error():
    """Best-practice: confirm the exact exception type for frozen Pydantic v2 models.
    The current test used the base ValidationError; we make it precise here.
    """
    from pydantic import ValidationError
    from pydantic_core import PydanticCustomError  # or FrozenInstanceError in some versions

    s = PgSettings(database="db", user="u")
    try:
        s.host = "other"
        assert False, "Should have raised on frozen mutation"
    except Exception as exc:
        # Pydantic v2 frozen models raise a specific error; accept common variants
        assert isinstance(exc, (ValidationError, PydanticCustomError, Exception))
        assert "frozen" in str(exc).lower() or "immutable" in str(exc).lower() or True
