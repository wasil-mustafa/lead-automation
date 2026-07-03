# Assigned-Company Names in the LF Name-Agreement Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LeadFeeder name-agreement gate accept candidates whose name agrees with any of the lead's kununu profile's *assigned-company* names (from the daily-cached kununu DWH feed), not just the profile name / email-domain root.

**Architecture:** New `src/leadauto/kununu_feed.py` downloads/caches the bulk `profiles.jsonl` feed (daily staleness) and streams it once per run to resolve the batch's profiles by `profile_uuid` (URL-slug fallback). `enrich.py` puts each resolved profile's assigned-company names into the LF match payload as `alt_names`; `client.py` scores agreement across all names (thresholds unchanged: 85/90), strips `alt_names` before the API POST, and reports which signal vouched (`agreed_via`) through `EnrichedLead` into a new QA Review column.

**Tech Stack:** Python 3.12, requests, rapidfuzz, pytest (pythonpath=src via pyproject.toml), typer CLI, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-07-03-assigned-companies-gate-design.md`

## Global Constraints

- Gate thresholds unchanged: `lf_name_agree_min: 85`, `lf_name_agree_rescue: 90` (config.yaml `matching:`).
- Every feed failure degrades to current behavior (no alt names); the pipeline never blocks on the feed.
- `alt_names` is internal metadata — it must NEVER appear in the LF POST body.
- Credentials only via env: `KUNUNU_API_TOKEN` (Bearer) or `KUNUNU_API_USER`/`KUNUNU_API_PASSWORD` (Basic). Never in yaml/code.
- Do NOT import `match.py` from `leadfeeder/` modules (import cycle via `enrich`); shared normalizers come from `textnorm.py`.
- The feed field holding assigned-company names is unconfirmed (`assigned_companies` vs `assigned_company_names`; list of strings vs dicts) — `_company_names()` handles all shapes and is the single place to fix after the first live pull.
- Run tests with `python -m pytest -q` from the repo root (Windows PowerShell; no `&&` chaining in PS 5.1 — use `;`).
- Work on branch `assigned-companies-gate` (already created; spec committed).

---

### Task 1: Config plumbing (settings, yaml, gitignore)

**Files:**
- Modify: `src/leadauto/config.py` (dataclass fields ~line 23-41, new properties after `lf_match_batch` ~line 91)
- Modify: `config/config.yaml` (new `kununu_feed:` block after the `leadfeeder:` block)
- Modify: `.gitignore` (feed cache files)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Settings` dataclass (`raw: dict`, `leadfeeder_api_key`, `sf_auth_url`) and its `load()` classmethod.
- Produces: `Settings.kununu_api_token/kununu_api_user/kununu_api_password` (str | None, env-sourced, default None); properties `kununu_feed_enabled: bool` (False when section missing), `kununu_feed_cache_path: Path` (resolved under `REPO_ROOT`), `kununu_feed_max_age_hours: int` (default 24), `kununu_feed_url: str` (default DWH URL). Tasks 3 and 5 rely on these exact names.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py -q`
Expected: 3 FAILED — `AttributeError: 'Settings' object has no attribute 'kununu_feed_enabled'` (and the credentials test fails on missing dataclass fields).

- [ ] **Step 3: Implement** — in `src/leadauto/config.py`:

Add dataclass fields (after `sf_auth_url: str | None`) with defaults so existing constructions keep working:

```python
    kununu_api_token: str | None = None
    kununu_api_user: str | None = None
    kununu_api_password: str | None = None
```

In `load()`, add to the `cls(...)` call:

```python
            kununu_api_token=os.environ.get("KUNUNU_API_TOKEN"),
            kununu_api_user=os.environ.get("KUNUNU_API_USER"),
            kununu_api_password=os.environ.get("KUNUNU_API_PASSWORD"),
```

Add properties after `lf_match_batch` (keep the section-comment style):

```python
    # --- kununu profile feed ---
    @property
    def kununu_feed_enabled(self) -> bool:
        return bool(self.raw.get("kununu_feed", {}).get("enabled", False))

    @property
    def kununu_feed_cache_path(self) -> Path:
        rel = self.raw.get("kununu_feed", {}).get("cache_path", "kununu_profiles.jsonl")
        return REPO_ROOT / rel

    @property
    def kununu_feed_max_age_hours(self) -> int:
        return int(self.raw.get("kununu_feed", {}).get("max_age_hours", 24))

    @property
    def kununu_feed_url(self) -> str:
        return self.raw.get("kununu_feed", {}).get(
            "url", "https://api.data.kununu.com/v1/feeds/data-management/profiles.jsonl")
```

In `config/config.yaml`, after the `leadfeeder:` block:

```yaml
# kununu DWH profile feed: resolves a lead's profile to its assigned-company names,
# which widen the LF name-agreement gate's evidence. Harmless without credentials
# (KUNUNU_API_TOKEN or KUNUNU_API_USER/KUNUNU_API_PASSWORD env) — the gate then
# behaves exactly as before.
kununu_feed:
  enabled: true
  cache_path: kununu_profiles.jsonl   # gitignored; ~530 MB when downloaded
  max_age_hours: 24                   # daily freshness (confirmed requirement)
```

In `.gitignore`, add:

```
kununu_profiles.jsonl
kununu_profiles.jsonl.part
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/config.py config/config.yaml .gitignore tests/test_config.py
git commit -m "feat(config): kununu profile feed settings + env credentials"
```

---

### Task 2: `kununu_feed.py` — slug + streaming lookup

**Files:**
- Create: `src/leadauto/kununu_feed.py`
- Test: `tests/test_kununu_feed.py` (new)

**Interfaces:**
- Consumes: `leadauto.logging.get_logger` (same pattern as other modules).
- Produces: `ProfileInfo` dataclass (`name: str`, `companies: list[str]`, `city: str | None`, `country_code: str | None`); `base_slug(url: str | None) -> str | None`; `lookup_profiles(feed_path, uuids, slugs, logger=None) -> dict[str, ProfileInfo]` keyed by BOTH uuid and slug. Task 3 adds `ensure_feed`/`load_profiles_for_leads` to this module; Task 5 imports `base_slug` and `load_profiles_for_leads`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_kununu_feed.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kununu_feed.py -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'leadauto.kununu_feed'`.

- [ ] **Step 3: Implement** — create `src/leadauto/kununu_feed.py`:

```python
"""kununu DWH profile feed: resolve leads' profiles to assigned-company names.

A kununu *main profile* can have several legal entities ("assigned companies") under
it. Their names are additional truthful identities for a lead and widen the LF
name-agreement gate's evidence (see the 2026-07-03 spec). The bulk feed (~530 MB
JSONL) is cached locally and refreshed daily; each run streams it ONCE, keeping only
the batch's profiles. Every failure path returns an empty result — the pipeline
degrades to pre-feed behavior, never blocks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger


@dataclass
class ProfileInfo:
    name: str
    companies: list[str] = field(default_factory=list)
    city: str | None = None
    country_code: str | None = None


def base_slug(url: str | None) -> str | None:
    """URL base slug: last path segment with the kununu dedup suffix removed.

    Dedup suffixes attach directly to a letter (siemens411 -> siemens); numbers
    after '-' are part of the name (promedis-24 stays). Same rule as the
    Profile-Matching repo's base_slug.
    """
    if not url:
        return None
    seg = url.rstrip("/").split("/")[-1].split("?")[0].strip().lower()
    if not seg:
        return None
    return re.sub(r"(?<=[a-z])\d+$", "", seg) or None


def _company_names(record: dict) -> list[str]:
    # Field name unconfirmed until the first live feed pull (the legacy analysis
    # script only read number_of_assigned_companies) — handle the plausible shapes;
    # this function is the single place to adjust afterwards.
    raw = record.get("assigned_companies") or record.get("assigned_company_names") or []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("name") or item.get("company_name")
        else:
            name = None
        if name and name.strip():
            names.append(name.strip())
    return names


def lookup_profiles(feed_path: str | Path, uuids: set, slugs: set,
                    logger=None) -> dict[str, ProfileInfo]:
    """One streaming pass over the feed; returns ProfileInfo keyed by uuid AND slug.

    Slug keys can be ambiguous across a dedup group (siemens, siemens1, ...): the
    first record wins, which is fine for alt-names — the group is one employer family.
    """
    log = logger or get_logger("kununu_feed")
    uuids = {u for u in uuids if u}
    slugs = {s for s in slugs if s}
    if not uuids and not slugs:
        return {}
    found: dict[str, ProfileInfo] = {}
    bad = 0
    with open(feed_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            uuid = d.get("profile_uuid")
            slug = base_slug(d.get("profile_url"))
            if uuid not in uuids and slug not in slugs:
                continue
            info = ProfileInfo(
                name=(d.get("profile_name") or "").strip(),
                companies=_company_names(d),
                city=d.get("profile_city"),
                country_code=d.get("profile_country_code"),
            )
            if uuid:
                found.setdefault(uuid, info)
            if slug:
                found.setdefault(slug, info)
    if bad:
        log.warning("kununu feed: skipped %d malformed line(s)", bad)
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kununu_feed.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/kununu_feed.py tests/test_kununu_feed.py
git commit -m "feat(kununu-feed): profile slug + one-pass streaming lookup"
```

---

### Task 3: `kununu_feed.py` — download/cache + per-batch loader

**Files:**
- Modify: `src/leadauto/kununu_feed.py`
- Test: `tests/test_kununu_feed.py`

**Interfaces:**
- Consumes: Task 1's `Settings.kununu_*` fields/properties; Task 2's `lookup_profiles`/`base_slug`.
- Produces: `ensure_feed(settings, logger=None) -> Path | None`; `load_profiles_for_leads(extracteds, settings, logger=None) -> dict[str, ProfileInfo]` (short-circuits to `{}` when no lead carries a uuid/slug — this keeps every existing enrich test off the filesystem). Task 5 calls `load_profiles_for_leads`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_kununu_feed.py`:

```python
import time
from types import SimpleNamespace

import requests

from leadauto import kununu_feed
from leadauto.kununu_feed import ensure_feed, load_profiles_for_leads
from leadauto.extract.models import ExtractedLead


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kununu_feed.py -q`
Expected: new tests FAIL — `ImportError: cannot import name 'ensure_feed'`.

- [ ] **Step 3: Implement** — append to `src/leadauto/kununu_feed.py` (add `import base64`, `import os`, `import time`, `import requests` to the imports):

```python
def _auth_headers(settings) -> dict | None:
    if settings.kununu_api_token:
        return {"Authorization": f"Bearer {settings.kununu_api_token}"}
    if settings.kununu_api_password is not None:
        cred = base64.b64encode(
            f"{settings.kununu_api_user or ''}:{settings.kununu_api_password}".encode()
        ).decode()
        return {"Authorization": f"Basic {cred}"}
    return None


def _download(url: str, dest: Path, headers: dict, log) -> None:
    log.info("Downloading kununu profile feed -> %s", dest)
    tmp = Path(f"{dest}.part")
    with requests.get(url, headers=headers, stream=True, timeout=600) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                out.write(chunk)
    os.replace(tmp, dest)


def ensure_feed(settings, logger=None) -> Path | None:
    """Cached feed path, refreshed when older than max_age_hours. None => no feed
    available this run (missing credentials / failed download with no cache)."""
    log = logger or get_logger("kununu_feed")
    path = settings.kununu_feed_cache_path
    max_age = settings.kununu_feed_max_age_hours * 3600
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age:
        return path
    headers = _auth_headers(settings)
    if headers is None:
        if path.exists():
            log.warning("kununu feed credentials missing; using stale cache %s", path)
            return path
        log.warning("kununu feed credentials missing and no cache; profile lookup skipped")
        return None
    try:
        _download(settings.kununu_feed_url, path, headers, log)
    except requests.exceptions.RequestException as err:
        if path.exists():
            log.warning("kununu feed download failed (%s); using stale cache", err)
            return path
        log.warning("kununu feed download failed (%s) and no cache; lookup skipped", err)
        return None
    return path


def load_profiles_for_leads(extracteds, settings, logger=None) -> dict[str, ProfileInfo]:
    """ProfileInfo for the batch, keyed by uuid and slug. {} on any unavailability."""
    log = logger or get_logger("kununu_feed")
    uuids = {e.kununu_profile_uuid for e in extracteds if e.kununu_profile_uuid}
    slugs = {base_slug(e.kununu_url) for e in extracteds if e.kununu_url}
    slugs.discard(None)
    if not uuids and not slugs:
        return {}
    feed = ensure_feed(settings, logger=log)
    if feed is None:
        return {}
    found = lookup_profiles(feed, uuids, slugs, logger=log)
    log.info("kununu feed: resolved %d of %d profile key(s)", len(found), len(uuids | slugs))
    return found
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kununu_feed.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/kununu_feed.py tests/test_kununu_feed.py
git commit -m "feat(kununu-feed): daily-cached download + per-batch profile loader"
```

---

### Task 4: Gate — alt names, `agreed_via`, POST strip

**Files:**
- Modify: `src/leadauto/leadfeeder/client.py:240-307` (`_name_agreement`, `_best_match`, `match_companies`)
- Test: `tests/test_leadfeeder_client.py`

**Interfaces:**
- Consumes: payload dicts that may carry `alt_names: list[str]`; `textnorm.normalize_name/alnum/domain_root` (already imported).
- Produces: `_name_agreement(matched_name, payload) -> tuple[int, str | None]` (second element: `"company_name"`, `"alt_name:<original name>"`, `"email_domain"`, or None); `_best_match` result dict gains `"agreed_via"`. Task 5 reads `agreed_via` from match results. `match_companies` strips `alt_names` before the POST.

- [ ] **Step 1: Update the two full-dict assertions** in `tests/test_leadfeeder_client.py` (they will otherwise fail once `agreed_via` is added):

In `test_match_company_picks_highest_score_above_threshold` replace the assert with:

```python
    assert match == {"company_id": "c2", "match_score": 0.95, "matched_name": "Acme GmbH",
                     "name_agreement": 100, "agreed_via": "company_name"}
```

In `test_match_companies_aligns_results_by_input_index` replace the assert with:

```python
    assert out == [{"company_id": "c1", "match_score": 0.95, "matched_name": "Acme",
                    "name_agreement": 100, "agreed_via": "company_name"}, None]
```

- [ ] **Step 2: Write the new failing tests** — append to the name-agreement-gate section of `tests/test_leadfeeder_client.py`:

```python
def test_match_company_rescued_by_alt_name():
    # Profile "Siemens", LF returns the assigned legal entity: alt name vouches.
    routes = {("POST", "/companies/match"):
              _match_payload([("c1", 0.30, "Siemens Mobility GmbH")])}
    client = _client(routes, min_match_score=0.70)
    match = client.match_companies([{
        "company_name": "Siemens", "email": "",
        "alt_names": ["Siemens Mobility GmbH", "Siemens AG"],
    }])[0]
    assert match["company_id"] == "c1"
    assert match["agreed_via"] == "alt_name:Siemens Mobility GmbH"
    assert match["name_agreement"] >= 90


def test_match_company_alt_names_do_not_loosen_gate():
    # Unrelated alt names must not admit a wrong candidate.
    routes = {("POST", "/companies/match"): _match_payload([("c1", 0.91, "GogoMil UG")])}
    client = _client(routes, min_match_score=0.70)
    assert client.match_companies([{
        "company_name": "myITplanet GmbH", "email": "",
        "alt_names": ["myITplanet Services GmbH"],
    }]) == [None]


def test_alt_names_are_stripped_from_post_body():
    seen = {}
    def handler(params, body):
        seen.update(body)
        return _match_payload([("c1", 0.95, "Acme GmbH")])
    routes = {("POST", "/companies/match"): handler}
    client = _client(routes, min_match_score=0.70)
    client.match_companies([{"company_name": "Acme", "alt_names": ["Acme Sub GmbH"]}])
    assert all("alt_names" not in c for c in seen["companies"])
```

(Check the test file's `_client`/`_match_payload` helpers: handlers receive `(params, body)` — if the existing handler signature differs, mirror the file's existing `handler = lambda p, b: ...` convention.)

- [ ] **Step 3: Run tests to verify the new ones fail**

Run: `python -m pytest tests/test_leadfeeder_client.py -q`
Expected: the Step 1 dict tests FAIL (no `agreed_via` yet), `test_match_company_rescued_by_alt_name` FAILS (alt names ignored -> None), `test_alt_names_are_stripped_from_post_body` FAILS (alt_names present in body).

- [ ] **Step 4: Implement** — in `src/leadauto/leadfeeder/client.py` replace `_name_agreement` with:

```python
    def _name_agreement(self, matched_name: str, payload: dict) -> tuple[int, str | None]:
        """(0-100 agreement, winning signal) between LF's returned name and the
        request's identity signals: the sent company_name, any alt_names (assigned
        companies of the lead's kununu profile), and — for corporate addresses — the
        email-domain root. (0, None) when LF returned no name (fail closed)."""
        cand = normalize_name(matched_name)
        if not cand:
            return 0, None
        best, via = 0, None
        ours = normalize_name(payload.get("company_name"))
        if ours:
            s = int(fuzz.token_set_ratio(cand, ours))
            if s > best:
                best, via = s, "company_name"
        for alt in payload.get("alt_names") or []:
            alt_norm = normalize_name(alt)
            if not alt_norm:
                continue
            s = int(fuzz.token_set_ratio(cand, alt_norm))
            if s > best:
                best, via = s, f"alt_name:{alt}"
        root = domain_root(payload.get("email"))
        if root:
            s = int(fuzz.partial_ratio(alnum(matched_name), root))
            if s > best:
                best, via = s, "email_domain"
        return best, via
```

In `_best_match`, change the agreement call and the result dict:

```python
            agree, via = self._name_agreement(summary.get("name", ""), payload)
```

```python
                best = {"company_id": m["id"], "match_score": score,
                        "matched_name": summary.get("name", ""), "name_agreement": agree,
                        "agreed_via": via}
```

In `match_companies`, strip `alt_names` before the POST (gating still uses the full `chunk[j]`):

```python
            api_chunk = [{k: v for k, v in p.items() if k != "alt_names"} for p in chunk]
            try:
                data = self.api_post(
                    "/companies/match", payload={"companies": api_chunk},
                    params={"account_id": account_id, "max_results_per_company": 3},
                )
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (existing gate tests unchanged in behavior — thresholds untouched).

- [ ] **Step 6: Commit**

```bash
git add src/leadauto/leadfeeder/client.py tests/test_leadfeeder_client.py
git commit -m "feat(leadfeeder): gate accepts assigned-company alt names, reports agreed_via"
```

---

### Task 5: Wiring — enrich, stage1, CLI flag

**Files:**
- Modify: `src/leadauto/leadfeeder/enrich.py` (EnrichedLead + phase 1)
- Modify: `src/leadauto/pipeline/stage1_enrich.py:58-59` (pass-through)
- Modify: `src/leadauto/cli.py` (stage1 `--kununu-feed/--no-kununu-feed`)
- Test: `tests/test_enrich.py`

**Interfaces:**
- Consumes: Task 3's `load_profiles_for_leads` + Task 2's `base_slug`/`ProfileInfo`; Task 4's `agreed_via` in match results; `textnorm.normalize_name`.
- Produces: `EnrichedLead.name_agreement` (exists) plus new `agreed_via: str | None`; `enrich_leads(client, extracteds, settings, logger=None, profiles=None, use_kununu_feed=True)` — `profiles` injects a prebuilt lookup (tests), `use_kununu_feed=False` disables the feed (CLI flag); `run_stage1(..., kununu_feed: bool = True)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_enrich.py`:

```python
from leadauto.kununu_feed import ProfileInfo


def test_enrich_puts_assigned_companies_into_alt_names():
    fc = FakeClient()
    fc.match_by_name = {"Siemens": {"company_id": "c1", "match_score": 0.30,
                                    "matched_name": "Siemens Mobility GmbH",
                                    "name_agreement": 96,
                                    "agreed_via": "alt_name:Siemens Mobility GmbH"}}
    fc.company_attrs = {"c1": {}}
    captured = {}
    orig = fc.match_companies
    def spy(payloads):
        captured["payloads"] = payloads
        return orig(payloads)
    fc.match_companies = spy
    e = _extracted(company="Siemens", contact_name=None)
    e.kununu_profile_uuid = "u1"
    profiles = {"u1": ProfileInfo(name="Siemens",
                                  companies=["Siemens Mobility GmbH", "Siemens", "Siemens AG"])}
    [r] = enrich_leads(fc, [e], SETTINGS, profiles=profiles)
    # own name (normalized-equal) excluded; order preserved; agreed_via lands on the lead
    assert captured["payloads"][0]["alt_names"] == ["Siemens Mobility GmbH", "Siemens AG"]
    assert r.agreed_via == "alt_name:Siemens Mobility GmbH"


def test_enrich_no_profile_found_leaves_payload_unchanged():
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    captured = {}
    orig = fc.match_companies
    def spy(payloads):
        captured["payloads"] = payloads
        return orig(payloads)
    fc.match_companies = spy
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS, profiles={})
    assert "alt_names" not in captured["payloads"][0]
    assert r.agreed_via is None


def test_enrich_use_kununu_feed_false_skips_lookup(monkeypatch):
    from leadauto.leadfeeder import enrich as enrich_mod
    monkeypatch.setattr(enrich_mod, "load_profiles_for_leads",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up")))
    fc = FakeClient()
    fc.match_by_name = {"Acme GmbH": {"company_id": "c1", "match_score": 0.9,
                                      "matched_name": "Acme GmbH"}}
    fc.company_attrs = {"c1": {}}
    [r] = enrich_leads(fc, [_extracted(contact_name=None)], SETTINGS, use_kununu_feed=False)
    assert r.matched is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_enrich.py -q`
Expected: FAIL — `enrich_leads() got an unexpected keyword argument 'profiles'` / no `agreed_via` attribute / no `load_profiles_for_leads` attribute on the module.

- [ ] **Step 3: Implement** — in `src/leadauto/leadfeeder/enrich.py`:

Imports: add

```python
from ..kununu_feed import base_slug, load_profiles_for_leads
from ..textnorm import normalize_name
```

`EnrichedLead`: after `name_agreement`, add

```python
    agreed_via: str | None = None  # which signal vouched: company_name / alt_name:<n> / email_domain
```

`enrich_leads` signature:

```python
def enrich_leads(client, extracteds: list[ExtractedLead], settings, logger=None,
                 profiles=None, use_kununu_feed: bool = True) -> list[EnrichedLead]:
```

After the empty-input early return, resolve profiles once for the batch:

```python
    if profiles is None and use_kununu_feed and settings.kununu_feed_enabled:
        profiles = load_profiles_for_leads(extracteds, settings, logger=log)
    profiles = profiles or {}
```

In the phase-1 payload loop, after `payload` is built (inside `if company_name:`), add the alt names:

```python
            info = (profiles.get(e.kununu_profile_uuid or "")
                    or profiles.get(base_slug(e.kununu_url) or ""))
            if info:
                own = normalize_name(company_name)
                alts = []
                for n in info.companies:
                    nn = normalize_name(n)
                    if nn and nn != own and n not in alts:
                        alts.append(n)
                if alts:
                    payload["alt_names"] = alts
```

In the match-result loop, after `r.name_agreement = ...`:

```python
        r.agreed_via = match.get("agreed_via")
```

In `src/leadauto/pipeline/stage1_enrich.py`: add `kununu_feed: bool = True` to `run_stage1`'s keyword-only params and change the enrich call:

```python
        enriched = enrich_leads(lf_client, extraction.leads, settings, logger=log,
                                use_kununu_feed=kununu_feed)
```

In `src/leadauto/cli.py` `stage1(...)`: add the option after `leadfeeder`:

```python
    kununu_feed: bool = typer.Option(True, "--kununu-feed/--no-kununu-feed",
                                     help="Disable the kununu profile feed (assigned-company alt names)."),
```

and pass `kununu_feed=kununu_feed` to `run_stage1`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass. (Existing enrich tests carry no kununu ids -> `load_profiles_for_leads` short-circuits without touching the filesystem.)

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/leadfeeder/enrich.py src/leadauto/pipeline/stage1_enrich.py src/leadauto/cli.py tests/test_enrich.py
git commit -m "feat(enrich): assigned-company alt names into LF payloads; agreed_via on EnrichedLead"
```

---

### Task 6: QA Review column `lf_agreed_via`

**Files:**
- Modify: `src/leadauto/qa_artifact.py:17-26` (REVIEW_COLUMNS) and `_review_row`
- Test: `tests/test_qa_artifact.py`

**Interfaces:**
- Consumes: Task 5's `EnrichedLead.agreed_via`.
- Produces: Review sheet column `lf_agreed_via` directly after `lf_name_agreement`.

- [ ] **Step 1: Write the failing test** — in `tests/test_qa_artifact.py`, add `agreed_via=None` to `_result`'s signature and pass it through to `EnrichedLead(...)` (next to `name_agreement=name_agreement`), then append:

```python
def test_review_row_shows_agreed_via_next_to_agreement():
    res = _result(matched=True, matched_name="Siemens Mobility GmbH", score=0.30,
                  name_agreement=96, agreed_via="alt_name:Siemens Mobility GmbH")
    ws = build_workbook([res], [], meta={})["Review"]
    idx = {h: i for i, h in enumerate(_headers(ws))}
    assert idx["lf_agreed_via"] == idx["lf_name_agreement"] + 1
    assert _rows(ws)[0][idx["lf_agreed_via"]] == "alt_name:Siemens Mobility GmbH"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_qa_artifact.py -q`
Expected: FAIL — `EnrichedLead` has the field (Task 5) so construction passes, but `lf_agreed_via` is missing from headers -> KeyError.

- [ ] **Step 3: Implement** — in `src/leadauto/qa_artifact.py`:

```python
    "lf_matched", "lf_matched_name", "lf_match_score", "lf_name_agreement", "lf_agreed_via",
```

and in `_review_row`:

```python
        "lf_match_score": enr.match_score, "lf_name_agreement": enr.name_agreement,
        "lf_agreed_via": enr.agreed_via,
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/leadauto/qa_artifact.py tests/test_qa_artifact.py
git commit -m "feat(qa): lf_agreed_via column on the Review sheet"
```

---

### Task 7: CI — feed cache + credential

**Files:**
- Modify: `.github/workflows/stage1.yml` (new steps before "Run Stage 1"; env addition)

**Interfaces:**
- Consumes: repo secret `KUNUNU_API_TOKEN` (to be set by the user — the workflow degrades gracefully until then); `ensure_feed`'s staleness logic.
- Produces: daily-keyed feed cache; `KUNUNU_API_TOKEN` in the Stage 1 env.

- [ ] **Step 1: Add the cache steps** — insert between "Install Salesforce CLI" and "Authenticate to Salesforce":

```yaml
      - name: Compute feed cache key
        id: feedkey
        run: echo "date=$(date -u +%F)" >> "$GITHUB_OUTPUT"

      - name: Cache kununu profile feed
        # Daily key: first run of the day restores yesterday's feed (stale), then
        # ensure_feed refreshes it and the post-job save stores it under today's key.
        # If the download fails, the stale restore keeps the gate working.
        uses: actions/cache@v4
        with:
          path: kununu_profiles.jsonl
          key: kununu-feed-${{ steps.feedkey.outputs.date }}
          restore-keys: kununu-feed-
```

- [ ] **Step 2: Add the credential to the Run Stage 1 env block:**

```yaml
          KUNUNU_API_TOKEN: ${{ secrets.KUNUNU_API_TOKEN }}
```

- [ ] **Step 3: Validate the workflow file**

Run: `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/stage1.yml').read_text(encoding='utf-8')); print('yaml ok')"`
Expected: `yaml ok`

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/stage1.yml
git commit -m "ci(stage1): daily-cached kununu profile feed + KUNUNU_API_TOKEN env"
```

---

### Task 8: Docs + final verification

**Files:**
- Modify: `CLAUDE.md` (Current status + Next steps)

**Interfaces:** none — documentation and the final gate.

- [ ] **Step 1: Run the full suite and record the count**

Run: `python -m pytest -q`
Expected: all pass (~175+; note the exact number).

- [ ] **Step 2: Update CLAUDE.md** — add to **Current status** (after the name-agreement-gate bullet):

```markdown
- ✅ **Assigned-company alt names in the LF gate** (spec `docs/superpowers/specs/2026-07-03-assigned-companies-gate-design.md`):
  `kununu_feed.py` streams the daily-cached DWH feed (join `profile_uuid`, URL-slug fallback) and
  `enrich.py` passes the profile's assigned-company names as `alt_names` (stripped before the LF POST);
  `_name_agreement` takes the max across profile name / alt names / email-domain root and reports
  `agreed_via` (QA column `lf_agreed_via`). Thresholds unchanged (85/90). Degrades to pre-feed behavior
  without credentials (`KUNUNU_API_TOKEN` env + GitHub secret; `--no-kununu-feed` to disable).
  ⚠️ NOT yet control-set-validated; the assigned-companies feed field name needs confirming on the
  first live pull (`kununu_feed._company_names`). <N> tests pass.
```

and to **Next steps**: `Validate alt-names gate on the 1,101-lead control set (needs KUNUNU_API_TOKEN + feed download) vs the 89.4%/1.1% baseline, confirm the feed's assigned-companies field name, set the GitHub secret.`

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: assigned-company alt-names gate status"
```

---

## Post-plan (live, needs credentials — NOT part of this plan)

1. Set `KUNUNU_API_TOKEN` locally (`.env` / env var) and as a GitHub repo secret.
2. First feed pull: confirm the assigned-companies field name in a real record; fix `_company_names` if needed.
3. Control-set re-run (1,101 leads) with alt names; compare to 89.4% accepted / 1.1% wrong-company; record in CLAUDE.md. This gates enabling in CI.
