"""Tests for the Phase 2b billing:manage scope step-up (auth.py)."""

from __future__ import annotations

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import (
    NOUS_BILLING_MANAGE_SCOPE,
    nous_token_has_billing_scope,
    step_up_nous_billing_scope,
)


# ---------------------------------------------------------------------------
# nous_token_has_billing_scope
# ---------------------------------------------------------------------------


def test_has_scope_true_when_present(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_provider_auth_state",
        lambda p: {"scope": "inference:invoke tool:invoke billing:manage"},
    )
    assert nous_token_has_billing_scope() is True


def test_has_scope_false_when_absent(monkeypatch):
    monkeypatch.setattr(
        auth, "get_provider_auth_state", lambda p: {"scope": "inference:invoke tool:invoke"}
    )
    assert nous_token_has_billing_scope() is False


def test_has_scope_false_when_no_state(monkeypatch):
    monkeypatch.setattr(auth, "get_provider_auth_state", lambda p: None)
    assert nous_token_has_billing_scope() is False


def test_has_scope_no_substring_false_positive(monkeypatch):
    # "billing:manage-lite" must NOT match billing:manage (split-based, not substring).
    monkeypatch.setattr(
        auth, "get_provider_auth_state", lambda p: {"scope": "billing:manage-lite"}
    )
    assert nous_token_has_billing_scope() is False


# ---------------------------------------------------------------------------
# step_up_nous_billing_scope
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_persist(monkeypatch):
    """Neutralize the persistence side-effects so step-up tests are pure."""
    monkeypatch.setattr(auth, "_auth_store_lock", lambda: _NullCtx())
    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(auth, "_save_provider_state", lambda *a, **kw: None)
    monkeypatch.setattr(auth, "_save_auth_store", lambda *a, **kw: "auth.json")
    monkeypatch.setattr(auth, "_write_shared_nous_state", lambda *a, **kw: None)
    monkeypatch.setattr(auth, "_sync_nous_pool_from_auth_store", lambda: None)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_step_up_requests_billing_scope_and_reuses_prior_urls(monkeypatch, _stub_persist):
    monkeypatch.setattr(
        auth,
        "get_provider_auth_state",
        lambda p: {
            "scope": "inference:invoke tool:invoke",
            "portal_base_url": "https://preview.example.com",
            "inference_base_url": "https://inf.example.com",
            "client_id": "hermes-cli",
        },
    )
    captured = {}

    def _fake_login(**kw):
        captured.update(kw)
        # Simulate the admin ticking the box → token comes back WITH the scope.
        return {"scope": "inference:invoke tool:invoke billing:manage", "access_token": "t"}

    monkeypatch.setattr(auth, "_nous_device_code_login", _fake_login)

    granted = step_up_nous_billing_scope()
    assert granted is True
    # Requested scope must include billing:manage, preserving prior scopes.
    assert NOUS_BILLING_MANAGE_SCOPE in captured["scope"].split()
    assert "inference:invoke" in captured["scope"].split()
    # Reuses the prior credential's deployment URLs (so a preview stays a preview).
    assert captured["portal_base_url"] == "https://preview.example.com"
    assert captured["client_id"] == "hermes-cli"


def test_step_up_returns_false_when_downscoped(monkeypatch, _stub_persist):
    # Non-admin / unticked → NAS silently downscopes; token comes back WITHOUT scope.
    monkeypatch.setattr(auth, "get_provider_auth_state", lambda p: {"scope": "inference:invoke"})
    monkeypatch.setattr(
        auth,
        "_nous_device_code_login",
        lambda **kw: {"scope": "inference:invoke", "access_token": "t"},
    )
    assert step_up_nous_billing_scope() is False


def test_step_up_falls_back_to_standard_scope_when_no_prior(monkeypatch, _stub_persist):
    monkeypatch.setattr(auth, "get_provider_auth_state", lambda p: {})
    captured = {}

    def _fake_login(**kw):
        captured.update(kw)
        return {"scope": "inference:invoke tool:invoke billing:manage"}

    monkeypatch.setattr(auth, "_nous_device_code_login", _fake_login)
    step_up_nous_billing_scope()
    requested = captured["scope"].split()
    assert "inference:invoke" in requested
    assert "tool:invoke" in requested
    assert NOUS_BILLING_MANAGE_SCOPE in requested
