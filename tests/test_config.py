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
    assert s.kununu_feed_cache_path.name == "kununu_profiles.jsonl"
    assert s.kununu_feed_max_age_hours == 24
    assert s.kununu_feed_url.startswith("https://api.data.kununu.com/")


def test_kununu_feed_defaults_when_section_missing():
    s = Settings(raw={}, leadfeeder_api_key=None, sf_auth_url=None)
    assert s.kununu_feed_enabled is False
    assert s.kununu_feed_max_age_hours == 24


def test_kununu_credentials_come_from_env(monkeypatch):
    monkeypatch.setenv("KUNUNU_API_TOKEN", "tok123")
    monkeypatch.setenv("KUNUNU_API_USER", "u")
    monkeypatch.setenv("KUNUNU_API_PASSWORD", "p")
    s = Settings.load()
    assert s.kununu_api_token == "tok123"
    assert s.kununu_api_user == "u"
    assert s.kununu_api_password == "p"


def test_thresholds_removed_and_dedupe_floor_present():
    s = Settings.load()
    assert s.dedupe_name_agree_min == 85
    assert not hasattr(s, "auto_link_threshold")
    assert not hasattr(s, "review_threshold")


def test_fresh_task_statuses_from_config_yaml():
    s = Settings.load()  # config.yaml lists the open/fresh statuses (excl. Abgeschlossen)
    assert s.fresh_task_statuses == ["In Progress", "in Arbeit", "Not Started"]


def test_fresh_task_statuses_defaults_empty_when_missing():
    s = Settings(raw={}, leadfeeder_api_key=None, sf_auth_url=None)
    assert s.fresh_task_statuses == []


def test_rohdaten_pool_owner_id_from_config():
    s = Settings(raw={"salesforce": {"rohdaten_pool_owner_id": "005Mz00000Xw0jjIAB"}},
                 leadfeeder_api_key=None, sf_auth_url=None)
    assert s.rohdaten_pool_owner_id == "005Mz00000Xw0jjIAB"


def test_rohdaten_pool_owner_id_absent_is_none():
    s = Settings(raw={"salesforce": {}}, leadfeeder_api_key=None, sf_auth_url=None)
    assert s.rohdaten_pool_owner_id is None
