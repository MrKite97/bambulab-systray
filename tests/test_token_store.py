"""Tests for src.token_store (keyring/DPAPI access-token storage).

Tests install an in-memory keyring backend so they are hermetic and
cross-platform: no real Windows Credential Manager entry is created during CI,
and the round-trip / idempotent-clear behavior is exercised without touching the
OS credential store.
"""

import keyring
import keyring.backend
import keyring.errors
import pytest

from src import token_store


class InMemoryKeyring(keyring.backend.KeyringBackend):
    """A minimal dict-backed keyring backend for hermetic tests."""

    priority = 1  # type: ignore[assignment]

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture(autouse=True)
def in_memory_keyring():
    """Swap in the in-memory backend for every test, restoring the real one after."""
    previous = keyring.get_keyring()
    keyring.set_keyring(InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


def test_save_then_load_round_trips():
    """Test 1: a saved token is retrievable on a later load."""
    token_store.save_token("abc")
    assert token_store.load_token() == "abc"


def test_clear_then_load_is_none():
    """Test 2: after clear, load returns None."""
    token_store.save_token("abc")
    token_store.clear_token()
    assert token_store.load_token() is None


def test_clear_on_absent_key_does_not_raise():
    """Test 3: clearing an already-absent token swallows PasswordDeleteError."""
    # No save first -- key is absent.
    token_store.clear_token()  # must not raise
    assert token_store.load_token() is None


def test_load_absent_token_is_none():
    """A never-saved token loads as None (not an empty string or error)."""
    assert token_store.load_token() is None
