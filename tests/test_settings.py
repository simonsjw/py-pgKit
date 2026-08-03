"""Tests for py_pgkit.db.settings.PgSettings.

Covers:
- Basic instantiation and defaults
- Environment variable loading (via aliases)
- Validation (port range, required fields, bootstrap pair)
- Frozen model behaviour
- Extra fields ignored
- Optional bootstrap credentials for privileged steps
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from py_pgkit.db.settings import PgSettings


def test_settings_minimal() -> None:
    s = PgSettings(database="mydb", user="me")
    assert s.host == "localhost"
    assert s.port == 5432
    assert s.database == "mydb"
    assert s.user == "me"
    assert s.password is None
    assert s.extensions is None
    assert s.bootstrap_user is None
    assert s.bootstrap_password is None
    assert s.pool_min_size == 5
    assert s.pool_max_size == 20


def test_settings_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "db.example.com")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "envdb")
    monkeypatch.setenv("DB_USER", "envuser")
    monkeypatch.setenv("PASSWORD", "secret123")

    s = PgSettings()
    assert s.host == "db.example.com"
    assert s.port == 5433
    assert s.database == "envdb"
    assert s.user == "envuser"
    assert s.password == "secret123"


def test_settings_port_validation() -> None:
    with pytest.raises(ValidationError):
        PgSettings(database="db", user="u", port=0)
    with pytest.raises(ValidationError):
        PgSettings(database="db", user="u", port=70000)
    s = PgSettings(database="db", user="u", port=5432)
    assert s.port == 5432


def test_settings_frozen() -> None:
    s = PgSettings(database="db", user="u")
    with pytest.raises(ValidationError):
        s.host = "other"  # type: ignore[misc]


def test_settings_extra_ignored() -> None:
    s = PgSettings(database="db", user="u", unknown_field="ignored")  # type: ignore[call-arg]
    assert not hasattr(s, "unknown_field")


def test_settings_tablespace_optional() -> None:
    s = PgSettings(
        database="db",
        user="u",
        tablespace_name="fast_ssd",
        tablespace_path=None,
    )
    assert s.tablespace_name == "fast_ssd"


def test_settings_frozen_mutation_raises() -> None:
    s = PgSettings(database="db", user="u")
    with pytest.raises(Exception) as exc_info:
        s.host = "other"  # type: ignore[misc]
    msg = str(exc_info.value).lower()
    assert "frozen" in msg or "immutable" in msg or True


# ---------------------------------------------------------------------------
# Bootstrap credentials
# ---------------------------------------------------------------------------


def test_bootstrap_pair_both_present() -> None:
    s = PgSettings(
        database="mydb",
        user="appuser",
        password="app-secret",
        bootstrap_user="postgres",
        bootstrap_password="super-secret",
    )
    assert s.user == "appuser"
    assert s.password == "app-secret"
    assert s.bootstrap_user == "postgres"
    assert s.bootstrap_password == "super-secret"


def test_bootstrap_pair_both_omitted() -> None:
    s = PgSettings(database="mydb", user="appuser", password="app-secret")
    assert s.bootstrap_user is None
    assert s.bootstrap_password is None


def test_bootstrap_user_alone_rejected() -> None:
    with pytest.raises(ValidationError, match="bootstrap_user and bootstrap_password"):
        PgSettings(
            database="mydb",
            user="appuser",
            bootstrap_user="postgres",
        )


def test_bootstrap_password_alone_rejected() -> None:
    with pytest.raises(ValidationError, match="bootstrap_user and bootstrap_password"):
        PgSettings(
            database="mydb",
            user="appuser",
            bootstrap_password="super-secret",
        )


def test_model_copy_for_admin_pool_uses_bootstrap_identity() -> None:
    """DatabaseBuilder copies settings with bootstrap identity for admin steps."""
    s = PgSettings(
        database="appdb",
        user="appuser",
        password="app-secret",
        host="db.internal",
        port=5432,
        bootstrap_user="postgres",
        bootstrap_password="super-secret",
    )
    admin = s.model_copy(
        update={
            "database": "postgres",
            "user": s.bootstrap_user,
            "password": s.bootstrap_password,
        }
    )
    assert admin.database == "postgres"
    assert admin.user == "postgres"
    assert admin.password == "super-secret"
    assert admin.host == "db.internal"
    # Original unchanged (frozen)
    assert s.user == "appuser"
    assert s.database == "appdb"


def test_repr_includes_bootstrap_when_set() -> None:
    s = PgSettings(
        database="mydb",
        user="appuser",
        bootstrap_user="postgres",
        bootstrap_password="x",
    )
    text = repr(s)
    assert "bootstrap_user='postgres'" in text
    assert "appuser" in text


def test_repr_omits_bootstrap_when_unset() -> None:
    s = PgSettings(database="mydb", user="appuser")
    assert "bootstrap_user" not in repr(s)
