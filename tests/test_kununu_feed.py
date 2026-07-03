"""kununu_feed tests: slug normalization + the one-pass streaming profile lookup."""
import json

from leadauto.kununu_feed import ProfileInfo, base_slug, lookup_profiles


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
