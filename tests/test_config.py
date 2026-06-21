"""Config/secrets resolution tests."""
from leadauto.config import Settings


def test_in_scope_subjects_reflects_enabled_only():
    s = Settings.load()  # config.yaml enables 'claimer' only
    assert s.in_scope_subjects == ["Inbound Sales Task: New Claimed Profile"]


def test_sf_auth_url_falls_back_to_sfdx_auth_url(monkeypatch):
    monkeypatch.delenv("SF_AUTH_URL", raising=False)
    monkeypatch.setenv("SFDX_AUTH_URL", "force://fallback")
    assert Settings.load().sf_auth_url == "force://fallback"


def test_sf_auth_url_prefers_sf_auth_url(monkeypatch):
    monkeypatch.setenv("SF_AUTH_URL", "force://primary")
    monkeypatch.setenv("SFDX_AUTH_URL", "force://secondary")
    assert Settings.load().sf_auth_url == "force://primary"
