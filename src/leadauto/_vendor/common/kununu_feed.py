"""kununu profiles feed client: cache location, auth, download, parse.

Extracted from main_profile_tag.py. The tagger's own graceful-degradation wrapper
(`get_kununu_profiles`, which turns an expected feed failure into a WARNING plus
`feed_ok=False` so daily mode can defer the feed-sensitive groups) deliberately
stays in the tagger: it encodes tagger policy, and the tagger's tests patch these
functions on the `main_profile_tag` module namespace.

FEED FORMAT (changed 2026-08-06): kununu switched the profiles feed from JSONL to
CSV. Every reader here handles BOTH and picks the parser by SNIFFING THE FILE
CONTENT, never the file extension or the URL - during a format switchover the
payload and the endpoint name are not guaranteed to change in lockstep, and a
cached file from the previous run may still be the old format. That also keeps a
rollback (point KUNUNU_FEED_URL back at the .jsonl endpoint) working with no code
change. The CSV shape is deliberately loosely specified; see
`_load_kununu_profiles_csv` for the column aliases and company-cell shapes
accepted, and `FeedSchemaError` for what happens when none of them match.

THREE READERS, ONE PARSER. Every consumer in the repo goes through one of these;
none may grow a private feed parser again:

    load_kununu_profiles(path, needed)  some UUIDs -> {name, companies, url}
                                        the tagger's cascade steps 11/12
    load_all_kununu_profiles(path)      every profile -> {name, companies, url}
                                        orphan_linker's UUID discovery
    iter_feed_records(path, needed)     whole records, any feed field
                                        profile_enricher/*, build_profiles_nosf_enriched

That rule is not tidiness. Until 2026-08-06 the three siblings each carried a
private JSONL-only loop while sharing the tagger's cache path - and the tagger
now downloads CSV. orphan_linker's loop swallowed unparseable lines with
`except json.JSONDecodeError: continue`, so a CSV feed would have produced {}
with feed_ok=True: a clean exit-0 run reporting zero matches, which its own
`if not feed_ok` guard could not catch because nothing had failed.
"""
import ast
import base64
import csv
import json
import os
import re
import shutil
import urllib.request

from . import REPO_ROOT


class FeedSchemaError(RuntimeError):
    """The feed was readable but carried none of the expected columns/fields.

    Subclasses RuntimeError DELIBERATELY: get_kununu_profiles already treats
    RuntimeError as an expected feed failure, so an unrecognised schema degrades
    to ({}, feed_ok=False) - the two name-match steps no-op AND daily mode defers
    every feed-sensitive group (FEED_SENSITIVE_REASONS) instead of reconciling
    them. That is the safe direction: on a feed we only half-understand, leaving
    real tags untouched beats churning them from a half-parsed file. The WARNING
    plus the deferred-group count are what tell a human to go look.
    """

# Cheap pre-filter for load_kununu_profiles: pulls profile_uuid out of a raw
# feed line without JSON-parsing it. >99% of the ~1M lines are for UUIDs we
# don't need, so fully parsing each one (nested company arrays included) just
# to discard it dominates the load time. A regex miss or false positive is
# harmless: the authoritative membership check still runs on the parsed record.
_PROFILE_UUID_RE = re.compile(r'"profile_uuid"\s*:\s*"([^"]+)"')

# How many leading lines _feed_format may look at before giving up. More than 1
# only so a stray blank/comment line at the top can't decide the format.
_SNIFF_MAX_LINES = 20

# CSV header aliases, matched against _norm_header (lowercased, trimmed,
# spaces/hyphens -> underscores). The first alias present wins. They are broader
# than today's known feed on purpose: the switchover is upstream's, the exact
# header text was not published, and a rename we can absorb costs nothing while
# a rename we can't costs a day of deferred groups.
_CSV_UUID_ALIASES = ("profile_uuid", "profileuuid", "uuid", "profile_id")
_CSV_NAME_ALIASES = ("profile_name", "profilename", "name", "main_profile_name")
# The kununu profile page URL (feed column `profile_url`). OPTIONAL, unlike the
# three above: it feeds no matching decision, only a reviewer-facing column, so
# a rename must never turn a readable feed into a FeedSchemaError. Missing ->
# the record's "url" is "".
_CSV_URL_ALIASES = ("profile_url", "profileurl", "url", "profile_link")
# A column holding ALL of a profile's assigned companies in one cell.
_CSV_COMPANY_LIST_ALIASES = (
    "assigned_companies", "assigned_company_names", "company_names", "companies",
)
# A column holding ONE company per row - i.e. the feed was exploded and a
# profile spans several rows. Handled by aggregating rows per profile_uuid.
_CSV_COMPANY_ONE_ALIASES = (
    "company_name", "assigned_company_name", "companyname",
)

# Cells that mean "no companies" in a flattened export (pandas writes NaN for a
# missing list; a JSON dump writes []).
_EMPTY_CELL_VALUES = frozenset({"", "[]", "{}", "nan", "none", "null", "n/a", "-"})

# Separators accepted in a plain (non-JSON) multi-company cell. Comma is
# deliberately NOT one of them: inside a CSV a comma is far more likely to be a
# quoted part of a single company name ("Muller, Meier & Co KG") than a
# separator, and splitting on it would fabricate company names that then feed
# EXACT name matching at cascade step 12.
_COMPANY_CELL_SEPARATORS = ("|", ";", "\n")

# csv default field limit is 128 KB; one cell holding every assigned company of
# a large profile group can exceed that, and the csv module raises rather than
# truncating. Raised only for the duration of the read (see _load_..._csv).
_CSV_FIELD_LIMIT = 50 * 1024 * 1024

# Cell -> scalar coercion for iter_feed_records. A CSV carries everything as
# text; the goal is to reproduce what the JSONL predecessor put in the same
# field, because the raw-record consumers write these values straight into a
# report (and build_profiles_nosf_enriched even --verifies against a workbook
# built from the JSONL). The rules below are MEASURED against the real
# 2026-08-01 JSONL, not assumed:
#   * counts/trends are real JSON ints            -> coerce
#   * is_managed_profile is a real JSON bool      -> coerce
#   * empty cells correspond to JSON null         -> coerce to None
#   * profile_score is a QUOTED STRING ("3.4")    -> do NOT coerce decimals,
#     or every score silently changes type on the .xlsx side
# Bounded to 15 digits so a long numeric-looking id keeps full precision.
_CELL_INT_RE = re.compile(r"^-?\d{1,15}$")
# "none" is deliberately NOT here: profile_contract_type carries the literal
# string "none" for an uncontracted profile, and nulling it would blank a real
# value in the enricher's report.
_CELL_NULL_VALUES = frozenset({"null", "nan"})

# Columns the JSONL feed ships as TEXT even though they look numeric, so the
# generic rules above must not touch them. profile_score is "3.4" (quoted) in
# the JSONL; in the CSV a whole-number score is the bare cell `5`, which the
# int rule would otherwise turn into 5 for ~8% of profiles while every decimal
# score stayed a string - the same column arriving as two different types.
_CELL_TEXT_COLUMNS = frozenset({"profile_score"})

# Default feed endpoint. The KUNUNU_FEED_URL env override is read lazily in
# download_feed (call time), consistent with KUNUNU_CACHE and the credentials -
# reading it at import time would silently ignore a .env loaded after import.
KUNUNU_FEED_URL_DEFAULT = (
    "https://api.data.kununu.com/v1/feeds/data-management/profiles.csv"
)

# Cache filenames we are willing to derive from the feed URL (see
# _kununu_cache_path). Anything else falls back to the default name rather than
# writing a ~530 MB file called e.g. "download?id=7".
_FEED_CACHE_EXTENSIONS = (".csv", ".jsonl", ".json")
_FEED_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FEED_CACHE_DEFAULT_NAME = "profiles.csv"

# Socket timeout (seconds) for the feed download. Without it, a kununu endpoint
# that connects but stalls mid-stream would hang the unattended daily run
# indefinitely - get_kununu_profiles's try/except cannot rescue a hang, only a
# raised error. A per-read timeout raises socket.timeout, which then degrades to
# {} through that same except (feed problems must never disrupt the run).
KUNUNU_FEED_TIMEOUT = 60


def load_kununu_profiles(path, needed_uuids):
    """Stream-parse the kununu profiles feed at `path` (CSV or JSONL - the format
    is sniffed from the content), keeping only the records whose profile_uuid is
    in `needed_uuids`.

    Returns {profile_uuid: {"name": profile_name, "companies": [company_name, ...],
    "url": profile_url}}, keyed by the LOWERCASED profile_uuid - the Salesforce<->feed UUID match is
    case-insensitive (and stripped) on both sides so a casing divergence between
    the two systems can't silently zero the name-match signals for a group.
    Streams row-by-row so the ~530 MB feed never fully loads into memory.

    Raises FeedSchemaError when the file is empty or (CSV) carries no recognisable
    uuid/name/company columns; the caller degrades that to feed_ok=False.
    """
    needed = {u for u in (str(x).strip().lower() for x in needed_uuids) if u}
    if not needed:
        return {}
    if _feed_format(path) == "csv":
        return _load_kununu_profiles_csv(path, needed)
    return _load_kununu_profiles_jsonl(path, needed)


def load_all_kununu_profiles(path):
    """Same shape as load_kununu_profiles, but for EVERY profile in the feed.

    The whole-feed consumers (orphan_linker's UUID discovery, profile_enricher)
    match orphans against all profiles rather than a needed subset, so there is
    no UUID pre-filter and no early exit. Shares the CSV/JSONL readers - and
    therefore the content sniffing - with the tagger's scoped loader, which is
    the point: a second private parser is how a format change ends up silently
    returning {} in one tool while the other works.
    """
    if _feed_format(path) == "csv":
        return _load_kununu_profiles_csv(path, None)
    return _load_kununu_profiles_jsonl(path, None)


def _feed_format(path):
    """Sniff "csv" or "jsonl" from the first non-blank line of `path`.

    Content, not extension: mid-switchover the endpoint name, the cached
    filename and the payload can disagree, and guessing from any of the first
    two would parse the feed with the wrong reader and silently yield {} - i.e.
    both name-match steps no-op with feed_ok=True, which is exactly the
    tag-churn the deferral machinery exists to prevent.

    A JSON line starts with '{' (or '[' for a pretty-printed array); anything
    else is treated as a CSV header. An empty file is a FeedSchemaError rather
    than an empty result, for the same reason: silently "no profiles matched"
    would look like a healthy run.
    """
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        for _ in range(_SNIFF_MAX_LINES):
            line = fh.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            return "jsonl" if stripped[0] in "{[" else "csv"
    raise FeedSchemaError(f"kununu feed at {path} is empty")


def _load_kununu_profiles_jsonl(path, needed):
    """JSONL reader: one JSON object per line. Malformed lines are skipped;
    stops early once every needed UUID is found. `needed` is already normalised
    to lowercase, non-empty strings by load_kununu_profiles.

    `needed=None` means EVERY profile (load_all_kununu_profiles): the regex
    pre-filter and the early exit both switch off, since neither has anything
    to test against, and blank-UUID records are dropped explicitly - the
    membership check used to do that for free."""
    out = {}
    want_all = needed is None
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if not want_all:
                m = _PROFILE_UUID_RE.search(line)
                if not m or m.group(1).strip().lower() not in needed:
                    continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            uuid = (d.get("profile_uuid") or "").strip().lower()
            if not uuid or (not want_all and uuid not in needed):
                continue
            companies = [
                (c.get("company_name") or "").strip()
                for c in (d.get("assigned_companies") or [])
                if (c.get("company_name") or "").strip()
            ]
            out[uuid] = {
                "name": (d.get("profile_name") or "").strip(),
                "companies": companies,
                "url": (d.get("profile_url") or "").strip(),
            }
            if not want_all and len(out) == len(needed):
                break
    return out


def _norm_header(raw):
    """Canonical form of a CSV header cell, for alias matching."""
    return (
        (raw or "")
        .strip()
        .strip('"')
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )


def _resolve_column(headers, aliases):
    """Index of the first alias present in `headers` ({norm_name: index})."""
    for alias in aliases:
        if alias in headers:
            return headers[alias]
    return None


def _resolve_company_columns(headers):
    """(list_column_index, single_column_index) for the assigned-company names.

    Exact aliases first; failing those, a last-resort scan for any header
    mentioning a company, so a rename we didn't anticipate still feeds cascade
    step 12 instead of silently zeroing it. uuid/id/count/number columns are
    excluded - matching those would put ids or digits into the name-match pool.
    Plural header -> one cell lists every company; singular -> one company per
    row (an exploded feed).
    """
    list_i = _resolve_column(headers, _CSV_COMPANY_LIST_ALIASES)
    one_i = _resolve_column(headers, _CSV_COMPANY_ONE_ALIASES)
    if list_i is not None or one_i is not None:
        return list_i, one_i
    for name, idx in headers.items():
        if "compan" not in name:
            continue
        if any(bad in name for bad in ("uuid", "_id", "id_", "count", "number")):
            continue
        if name.endswith("s") or "names" in name:
            return idx, None
        return None, idx
    return None, None


def _company_names_from_obj(obj):
    """Flatten a parsed companies cell into a list of company-name strings.

    Accepts the shapes a flattened export can produce: a list of dicts (the
    JSONL feed's assigned_companies, dumped verbatim), a list of plain strings,
    a single dict, or a bare string.
    """
    if isinstance(obj, str):
        return [obj.strip()] if obj.strip() else []
    if isinstance(obj, dict):
        for key in ("company_name", "name", "companyName"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return [val.strip()]
        return []
    if isinstance(obj, (list, tuple)):
        out = []
        for item in obj:
            out.extend(_company_names_from_obj(item))
        return out
    return []


def _company_names_from_cell(cell, split=True):
    """Company names held in one CSV cell.

    Handles, in order: an empty/NaN marker; a JSON array (["A","B"] or the
    feed's [{"company_name": "A"}, ...]); the same array as a PYTHON repr, which
    is what a pandas dump of a nested column produces ([{'company_name': 'A'}] -
    note the single quotes, which json.loads rejects); a separator-delimited
    list; otherwise the cell is one company name.

    `split=False` for a one-company-per-row column: there the cell IS a single
    name, and splitting it would corrupt any name containing a ';' or '|'.
    """
    text = (cell or "").strip()
    if text.lower() in _EMPTY_CELL_VALUES:
        return []
    if text[0] in "[{":
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                parsed = None
        if parsed is not None:
            return [n for n in _company_names_from_obj(parsed) if n]
    if split:
        for sep in _COMPANY_CELL_SEPARATORS:
            if sep in text:
                return [p.strip() for p in text.split(sep) if p.strip()]
    return [text]


def _load_kununu_profiles_csv(path, needed):
    """CSV reader, the format kununu switched to on 2026-08-06.

    Same contract as the JSONL reader: {lowercased profile_uuid: {"name",
    "companies", "url"}} for the requested UUIDs only, streamed row by row.

    Two row shapes are supported and detected from the header:
      * one row per profile, all assigned companies in one cell (any of the
        shapes _company_names_from_cell accepts);
      * an EXPLODED feed - one row per (profile, company) pair - whose rows are
        aggregated per profile_uuid, companies de-duplicated in first-seen
        order. The early exit is disabled in this shape: a profile's remaining
        company rows can appear anywhere later in the file, and stopping at the
        first sighting would silently truncate its company list.

    `needed` is already normalised to lowercase, non-empty strings by
    load_kununu_profiles. Rows too short to hold the uuid column are skipped
    (a ragged export line), which mirrors the JSONL reader skipping bad lines.

    `needed=None` means EVERY profile (load_all_kununu_profiles): the membership
    test and the early exit switch off, and a blank uuid cell is dropped
    explicitly - the membership check used to do that for free.
    """
    out = {}
    seen_companies = {}
    prev_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(_CSV_FIELD_LIMIT)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise FeedSchemaError(f"kununu CSV feed at {path} has no header row")
            headers = {}
            for idx, raw in enumerate(header):
                name = _norm_header(raw)
                if name and name not in headers:
                    headers[name] = idx
            uuid_i = _resolve_column(headers, _CSV_UUID_ALIASES)
            name_i = _resolve_column(headers, _CSV_NAME_ALIASES)
            url_i = _resolve_column(headers, _CSV_URL_ALIASES)
            list_i, one_i = _resolve_company_columns(headers)
            if uuid_i is None or name_i is None or (list_i is None and one_i is None):
                missing = [
                    label
                    for label, found in (
                        ("profile uuid", uuid_i is not None),
                        ("profile name", name_i is not None),
                        ("assigned companies", list_i is not None or one_i is not None),
                    )
                    if not found
                ]
                raise FeedSchemaError(
                    f"kununu CSV feed at {path} has no recognisable "
                    f"{', '.join(missing)} column(s); header was: "
                    f"{[_norm_header(h) for h in header[:25]]}"
                )
            exploded = list_i is None
            want_all = needed is None
            for row in reader:
                if uuid_i >= len(row):
                    continue
                uuid = row[uuid_i].strip().lower()
                if not uuid or (not want_all and uuid not in needed):
                    continue
                rec = out.get(uuid)
                if rec is None:
                    rec = out[uuid] = {"name": "", "companies": [], "url": ""}
                    seen = seen_companies[uuid] = set()
                else:
                    seen = seen_companies[uuid]
                if not rec["name"] and name_i < len(row):
                    rec["name"] = row[name_i].strip()
                if not rec["url"] and url_i is not None and url_i < len(row):
                    rec["url"] = row[url_i].strip()
                if list_i is not None and list_i < len(row):
                    names = _company_names_from_cell(row[list_i])
                elif one_i is not None and one_i < len(row):
                    names = _company_names_from_cell(row[one_i], split=False)
                else:
                    names = []
                for company in names:
                    if company and company not in seen:
                        seen.add(company)
                        rec["companies"].append(company)
                if not exploded and not want_all and len(out) == len(needed):
                    break
    finally:
        csv.field_size_limit(prev_limit)
    return out


def _coerce_cell(text, column=None):
    """Best-effort JSON scalar for one CSV cell, so a record read from the CSV
    feed matches what the JSONL feed carried for the same field.

    `column` is the normalised header name, used only to leave
    _CELL_TEXT_COLUMNS verbatim.

    Empty/null/NaN -> None, true/false -> bool, a bracketed cell -> the parsed
    list or dict (JSON first, then a Python repr - what a pandas dump of the
    nested assigned_companies column produces), a bare integer -> int,
    anything else -> the trimmed string.

    DECIMALS AND DATES STAY STRINGS, matching the JSONL feed: `profile_score`
    ships there as the quoted string "3.4", so parsing it to a float here would
    change the type of a column the enrichers write into a spreadsheet. See the
    measured rules at _CELL_INT_RE.

    Known, accepted imprecision: a profile or company name that is *entirely*
    digits comes back as an int. It only affects the raw-record consumers'
    report columns (never name matching, which goes through the
    load_*_kununu_profiles readers and keeps text verbatim).
    """
    s = (text or "").strip()
    if not s:
        return None
    if column in _CELL_TEXT_COLUMNS:
        return s
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in _CELL_NULL_VALUES:
        return None
    if s[0] in "[{":
        try:
            return json.loads(s)
        except ValueError:
            try:
                return ast.literal_eval(s)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return s
    if _CELL_INT_RE.match(s):
        return int(s)
    return s


def _normalize_company_list(val):
    """Coerce whatever a CSV assigned-companies cell parsed into onto the shape
    the JSONL feed carries: a list of dicts, each with at least company_name.

    Guards the one field the raw-record consumers index INTO (`c["company_name"]`
    / `c["company_uuid"]`). A cell that opens with '[' but parses as neither JSON
    nor a Python repr comes back from _coerce_cell as the raw string, and
    iterating that would hand each consumer single characters and an
    AttributeError. Empty list is the honest answer for a cell we can't read -
    the profile keeps its name, it just contributes no company names.
    """
    if isinstance(val, dict):
        val = [val]
    if not isinstance(val, list):
        return []
    out = []
    for item in val:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({"company_name": item.strip()})
    return out


def iter_feed_records(path, needed=None):
    """Stream `(lowercased profile_uuid, record)` pairs from the feed at `path`,
    format sniffed from the content exactly as load_kununu_profiles does.

    `record` is a dict keyed by the feed's own field names. For JSONL it is the
    parsed object verbatim; for CSV each row is rebuilt into the same shape via
    _coerce_cell, so `rec["profile_score"]` is a float and
    `rec["assigned_companies"][0]["company_name"]` resolves on BOTH formats.
    This is what the whole-record consumers (the enrichers and the admin-status
    report) need and load_kununu_profiles deliberately does not provide - it
    projects down to {name, companies} for the tagger's cascade.

    `needed=None` streams every record; otherwise it is an iterable of
    profile_uuids matched case-insensitively, and the reader stops as soon as
    all of them are found. At most one record is yielded per uuid.

    Raises FeedSchemaError on an empty file, a CSV with no recognisable uuid
    column, or a CSV in the EXPLODED shape (one company per row): rebuilding a
    whole record from rows that repeat every other column is not something this
    reader guesses at. The scoped/whole-feed {name, companies} readers do handle
    that shape - a consumer needing both should go through those.
    """
    if needed is not None:
        needed = {u for u in (str(x).strip().lower() for x in needed) if u}
        if not needed:
            return
    if _feed_format(path) == "jsonl":
        yield from _iter_feed_records_jsonl(path, needed)
    else:
        yield from _iter_feed_records_csv(path, needed)


def _iter_feed_records_jsonl(path, needed):
    seen = set()
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if needed is not None:
                m = _PROFILE_UUID_RE.search(line)
                if not m or m.group(1).strip().lower() not in needed:
                    continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            uuid = (rec.get("profile_uuid") or "").strip().lower()
            if not uuid or uuid in seen:
                continue
            if needed is not None and uuid not in needed:
                continue
            seen.add(uuid)
            yield uuid, rec
            if needed is not None and len(seen) == len(needed):
                return


def _iter_feed_records_csv(path, needed):
    seen = set()
    prev_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(_CSV_FIELD_LIMIT)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise FeedSchemaError(f"kununu CSV feed at {path} has no header row")
            names = [_norm_header(h) for h in header]
            headers = {}
            for idx, name in enumerate(names):
                if name and name not in headers:
                    headers[name] = idx
            uuid_i = _resolve_column(headers, _CSV_UUID_ALIASES)
            if uuid_i is None:
                raise FeedSchemaError(
                    f"kununu CSV feed at {path} has no recognisable profile uuid "
                    f"column; header was: {names[:25]}"
                )
            list_i, one_i = _resolve_company_columns(headers)
            if list_i is None and one_i is not None:
                raise FeedSchemaError(
                    f"kununu CSV feed at {path} is in the exploded "
                    "one-company-per-row shape, which the whole-record reader "
                    "does not support"
                )
            company_col = names[list_i] if list_i is not None else None
            for row in reader:
                if uuid_i >= len(row):
                    continue
                uuid = row[uuid_i].strip().lower()
                if not uuid or uuid in seen:
                    continue
                if needed is not None and uuid not in needed:
                    continue
                seen.add(uuid)
                # Built from `headers`, not enumerate(names): headers keeps the
                # FIRST index for a repeated header, which is the one uuid_i /
                # list_i were resolved against. Enumerating instead would let a
                # duplicate column later in the row overwrite the value the
                # company/uuid resolution is pointing at.
                rec = {
                    name: _coerce_cell(row[i], name)
                    for name, i in headers.items()
                    if i < len(row)
                }
                rec["profile_uuid"] = row[uuid_i].strip()
                if company_col is not None:
                    rec[company_col] = _normalize_company_list(rec.get(company_col))
                yield uuid, rec
                if needed is not None and len(seen) == len(needed):
                    return
    finally:
        csv.field_size_limit(prev_limit)


def _kununu_cache_path():
    """Local path where the downloaded feed is cached. Override with KUNUNU_CACHE.

    The default is anchored on REPO_ROOT, not this module's `__file__`: the
    function used to live in main_profile_tag.py at the repo root, so a
    `__file__`-relative default would now point at common/profiles.jsonl (see the
    package docstring). Every feed consumer in the repo resolves its path through
    here or through KUNUNU_CACHE - keep it that way.

    The FILENAME follows the feed URL (profiles.csv today, profiles.jsonl if
    KUNUNU_FEED_URL is pointed back at the old endpoint) so the cache never
    claims a format the payload isn't: one env var switches both ends, and a
    format change downloads to a NEW path instead of appearing to reuse a cache
    written in the old format. Parsing itself never trusts this name - it sniffs
    the content (see _feed_format)."""
    return os.environ.get(
        "KUNUNU_CACHE",
        str(REPO_ROOT / _feed_cache_name()),
    )


def _feed_cache_name():
    """Cache filename derived from the feed URL's last path segment.

    Query string stripped; only a plain name with a known feed extension is
    accepted, so a URL that carries the filename somewhere unexpected falls back
    to the default rather than producing a junk (or path-traversing) filename."""
    url = os.environ.get("KUNUNU_FEED_URL", KUNUNU_FEED_URL_DEFAULT)
    candidate = url.split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if (
        _FEED_CACHE_NAME_RE.match(candidate)
        and candidate.lower().endswith(_FEED_CACHE_EXTENSIONS)
    ):
        return candidate
    return _FEED_CACHE_DEFAULT_NAME


def _kununu_auth_header():
    """Authorization header for the feed API: HTTP Basic from KUNUNU_API_USER
    (default empty) / KUNUNU_API_PASSWORD. Returns a dict, or None if no
    credential is configured.

    Deliberately does NOT read KUNUNU_API_TOKEN: the feed uses Basic
    (password-only), and the repo secret of that name holds the Basic password,
    not a Bearer token. A Bearer branch keyed on KUNUNU_API_TOKEN was removed
    because anyone naturally exporting that env var locally (or wiring the
    secret straight through in CI) would send "Bearer <basic-password>" ->
    401 -> silent degradation to {}."""
    pw = os.environ.get("KUNUNU_API_PASSWORD")
    if pw is not None:
        user = os.environ.get("KUNUNU_API_USER", "")
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        return {"Authorization": "Basic " + cred}
    return None


def download_feed(dest):
    """Stream the kununu feed to `dest` (atomic via a .part temp file). Raises on
    missing credentials or any HTTP/network error."""
    hdr = _kununu_auth_header()
    if hdr is None:
        raise RuntimeError("no kununu credentials (set KUNUNU_API_PASSWORD)")
    url = os.environ.get("KUNUNU_FEED_URL", KUNUNU_FEED_URL_DEFAULT)
    req = urllib.request.Request(url, headers=hdr)
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=KUNUNU_FEED_TIMEOUT) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    os.replace(tmp, dest)
