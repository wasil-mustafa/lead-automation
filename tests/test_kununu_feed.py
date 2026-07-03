"""kununu_feed tests: slug normalization + the one-pass streaming profile lookup."""
import json
import time
from types import SimpleNamespace

import requests

from leadauto import kununu_feed
from leadauto.kununu_feed import ProfileInfo, base_slug, lookup_profiles, ensure_feed, load_profiles_for_leads
from leadauto.extract.models import ExtractedLead


def _write_feed(tmp_path, records, extra_lines=()):
    p = tmp_path / "profiles.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    lines.extend(extra_lines)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _record(uuid="u1", name="Siemens", url="https://www.kununu.com/de/siemens",
            companies=None, **extra):
    d = {"profile_uuid": uuid, "profile_name": name, "profile_url": url,
         "profile_city": "Muenchen", "profile_country_code": "de"}
    if companies is not None:
        d["assigned_companies"] = companies
    d.update(extra)
    return d


# --- base_slug ---------------------------------------------------------------

def test_base_slug_strips_dedup_suffix():
    # kununu dedup suffixes attach directly to a letter (siemens411 -> siemens) ...
    assert base_slug("https://www.kununu.com/de/siemens411") == "siemens"


def test_base_slug_keeps_hyphenated_numbers():
    # ... but numbers after '-' are part of the name (promedis-24).
    assert base_slug("https://www.kununu.com/de/promedis-24") == "promedis-24"


def test_base_slug_handles_trailing_slash_and_none():
    assert base_slug("https://www.kununu.com/de/acme/") == "acme"
    assert base_slug(None) is None
    assert base_slug("") is None


# --- lookup_profiles ----------------------------------------------------------

def test_lookup_by_uuid(tmp_path):
    feed = _write_feed(tmp_path, [_record(companies=["Siemens Mobility GmbH", "Siemens AG"])])
    found = lookup_profiles(feed, uuids={"u1"}, slugs=set())
    assert found["u1"].name == "Siemens"
    assert found["u1"].companies == ["Siemens Mobility GmbH", "Siemens AG"]
    assert found["u1"].country_code == "de"


def test_lookup_by_slug_fallback(tmp_path):
    feed = _write_feed(tmp_path, [_record(uuid="u9", url="https://www.kununu.com/de/acme3")])
    found = lookup_profiles(feed, uuids=set(), slugs={"acme"})
    assert "acme" in found and found["acme"].name == "Siemens"


def test_lookup_miss_and_empty_keys(tmp_path):
    feed = _write_feed(tmp_path, [_record()])
    assert lookup_profiles(feed, uuids={"nope"}, slugs={"nope"}) == {}
    assert lookup_profiles(feed, uuids=set(), slugs=set()) == {}


def test_lookup_skips_malformed_lines(tmp_path):
    feed = _write_feed(tmp_path, [_record()], extra_lines=["{not json", ""])
    found = lookup_profiles(feed, uuids={"u1"}, slugs=set())
    assert "u1" in found


def test_lookup_missing_feed_file_returns_empty(tmp_path):
    # The module promises degradation, never an exception, on any failure path.
    assert lookup_profiles(tmp_path / "nope.jsonl", uuids={"u1"}, slugs=set()) == {}


def test_company_names_field_shapes(tmp_path):
    # The exact feed field is unconfirmed; both list-of-strings and list-of-dicts work.
    feed = _write_feed(tmp_path, [
        _record(uuid="a", url="https://www.kununu.com/de/a", companies=["Plain GmbH"]),
        _record(uuid="b", url="https://www.kununu.com/de/b",
                companies=[{"name": "Dict AG"}, {"company_name": "Alt Key SE"}, {"x": 1}]),
        _record(uuid="c", url="https://www.kununu.com/de/c",
                assigned_company_names=["Other Field GmbH"]),
    ])
    found = lookup_profiles(feed, uuids={"a", "b", "c"}, slugs=set())
    assert found["a"].companies == ["Plain GmbH"]
    assert found["b"].companies == ["Dict AG", "Alt Key SE"]
    assert found["c"].companies == ["Other Field GmbH"]


# --- ensure_feed + load_profiles_for_leads -----------------------------------

def _settings(tmp_path, token="tok", enabled=True, max_age=24):
    return SimpleNamespace(
        kununu_feed_enabled=enabled,
        kununu_feed_cache_path=tmp_path / "profiles.jsonl",
        kununu_feed_max_age_hours=max_age,
        kununu_feed_url="https://feed.example/profiles.jsonl",
        kununu_api_token=token, kununu_api_user=None, kununu_api_password=None,
    )


def test_ensure_feed_fresh_cache_skips_download(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.kununu_feed_cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kununu_feed, "_download",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("downloaded")))
    assert ensure_feed(s) == s.kununu_feed_cache_path


def test_ensure_feed_stale_cache_triggers_download(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.kununu_feed_cache_path.write_text("{}", encoding="utf-8")
    old = time.time() - 25 * 3600
    import os
    os.utime(s.kununu_feed_cache_path, (old, old))
    called = {}
    monkeypatch.setattr(kununu_feed, "_download", lambda url, dest, hdr, log: called.update(url=url))
    assert ensure_feed(s) == s.kununu_feed_cache_path
    assert called["url"] == s.kununu_feed_url


def test_ensure_feed_download_failure_falls_back_to_stale(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.kununu_feed_cache_path.write_text("{}", encoding="utf-8")
    old = time.time() - 25 * 3600
    import os
    os.utime(s.kununu_feed_cache_path, (old, old))
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(kununu_feed, "_download", boom)
    assert ensure_feed(s) == s.kununu_feed_cache_path  # stale beats nothing


def test_ensure_feed_no_credentials_no_cache_returns_none(tmp_path):
    assert ensure_feed(_settings(tmp_path, token=None)) is None


def test_load_profiles_for_leads_short_circuits_without_keys(tmp_path, monkeypatch):
    # No lead carries a uuid/slug -> never touches the feed (keeps unit tests hermetic).
    monkeypatch.setattr(kununu_feed, "ensure_feed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("touched feed")))
    lead = ExtractedLead(lead_id="00Q1", task_id="T1", task_type="claimer")
    assert load_profiles_for_leads([lead], _settings(tmp_path)) == {}


def test_load_profiles_for_leads_resolves_uuid_and_slug(tmp_path):
    s = _settings(tmp_path)
    records = [
        _record(uuid="u1", name="Siemens", companies=["Siemens Mobility GmbH"]),
        _record(uuid="u2", name="Acme", url="https://www.kununu.com/de/acme2"),
    ]
    s.kununu_feed_cache_path.write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8")
    leads = [
        ExtractedLead(lead_id="1", task_id="T1", task_type="claimer", kununu_profile_uuid="u1"),
        ExtractedLead(lead_id="2", task_id="T2", task_type="claimer",
                      kununu_url="https://www.kununu.com/de/acme7"),
    ]
    found = load_profiles_for_leads(leads, s)
    assert found["u1"].companies == ["Siemens Mobility GmbH"]
    assert found["acme"].name == "Acme"


def test_download_failure_cleans_up_part_file(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    class FakeResp:
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield b"data"
            raise requests.exceptions.ChunkedEncodingError("dropped")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(kununu_feed.requests, "get", lambda *a, **k: FakeResp())
    assert ensure_feed(s) is None  # no pre-existing cache to fall back to
    assert not (tmp_path / "profiles.jsonl.part").exists()


def test_load_profiles_for_leads_swallows_lookup_errors(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.kununu_feed_cache_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kununu_feed, "lookup_profiles",
                        lambda *a, **k: (_ for _ in ()).throw(UnicodeDecodeError(
                            "utf-8", b"\xff", 0, 1, "invalid start byte")))
    lead = ExtractedLead(lead_id="1", task_id="T1", task_type="claimer",
                         kununu_profile_uuid="u1")
    assert load_profiles_for_leads([lead], s) == {}
