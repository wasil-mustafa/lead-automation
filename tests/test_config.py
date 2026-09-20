"""Config/secrets resolution tests."""
from leadauto.config import Settings


def test_in_scope_subjects_reflects_enabled_only():
    s = Settings.load()  # config.yaml enables 'claimer' + 'ebp_trial' (simplified scope)
    assert s.in_scope_subjects == [
        "Inbound Sales Task: New Claimed Profile",
        "Inbound Sales Task: EBP Trial",
    ]


def test_sf_auth_url_falls_back_to_sfdx_auth_url(monkeypatch):
    monkeypatch.delenv("SF_AUTH_URL", raising=False)
    monkeypatch.setenv("SFDX_AUTH_URL", "force://fallback")
    assert Settings.load().sf_auth_url == "force://fallback"


def test_sf_auth_url_prefers_sf_auth_url(monkeypatch):
    monkeypatch.setenv("SF_AUTH_URL", "force://primary")
    monkeypatch.setenv("SFDX_AUTH_URL", "force://secondary")
    assert Settings.load().sf_auth_url == "force://primary"


def test_leadfeeder_throttle_and_batch_defaults():
    s = Settings.load()
    assert s.lf_max_requests_per_min == 95
    assert s.lf_max_retries == 4
    assert s.lf_retrieve_batch == 100
    assert s.lf_match_batch == 200


def test_kununu_feed_settings_from_config_yaml():
    s = Settings.load()  # config.yaml enables the feed with defaults
    assert s.kununu_feed_enabled is True
    assert s.kununu_feed_max_age_hours == 24
    # The endpoint and the cache path are NOT configured here any more — they belong to
    # `common.kununu_feed`. Both used to be hardcoded to `profiles.jsonl`, which has 404'd
    # since kununu switched the feed to CSV on 2026-08-06, and every run degraded to "no
    # profiles" behind one WARNING. A default here is how that drifts again.
    assert s.kununu_feed_cache_path is None


def test_kununu_feed_defaults_when_section_missing():
    s = Settings(raw={}, leadfeeder_api_key=None, sf_auth_url=None)
    assert s.kununu_feed_enabled is False
    assert s.kununu_feed_max_age_hours == 24


