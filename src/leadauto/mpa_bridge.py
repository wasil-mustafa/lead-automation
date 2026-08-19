"""`sys.path` bridge to the Main-Profile-Account checkout, so this repo can import
its shared, domain-free infrastructure package:

    from leadauto.mpa_bridge import common_kununu_feed
    feed = common_kununu_feed()          # the `common.kununu_feed` module

Path resolution: the `MPA_ROOT` env var, else the sibling directory
`../Main-Profile-Account`. Derived, never hardcoded to a user profile — the two
laptops that work these projects have different profile names.

The real checkout ALWAYS wins. When it is absent, `common_kununu_feed()` falls back
to the byte-identical snapshot in `leadauto/_vendor/common/` and says so at WARNING
level — added 2026-08-19, because CI cannot reach the private Main-Profile-Account
repo (the `GITHUB_TOKEN` Actions injects is scoped to this repository, and the
alternatives were a classic PAT good for every repo its owner can see, or a deploy
key on someone else's repo). Local runs — and any runner given an `MPA_ROOT` — therefore
execute the canonical module; the snapshot is a CI backstop, not a second reader.

WHAT MUST NOT HAPPEN is a copy nobody checks. Requesting a dead `.jsonl` endpoint for
eleven days after kununu switched the feed to CSV on 2026-08-06 — degrading to "no
profiles", and so to no `profile_score`, which fails the live-scope gate closed —
cost nothing but a single WARNING to notice, while Main-Profile-Account followed the
switchover the same day. The snapshot is kept honest by `tests/test_mpa_vendor.py`,
which hashes it against the live files and fails on any difference wherever both
checkouts exist. Never edit the vendored files: fix it upstream and re-sync
(`leadauto/_vendor/PROVENANCE.md`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .logging import get_logger
from .state import REPO_ROOT

_DEFAULT_SIBLING = "Main-Profile-Account"

#: The vendored snapshot's package root — `_vendor/` goes on `sys.path`, so the
#: snapshot imports as plain `common.kununu_feed`, exactly as upstream does. That
#: keeps the copied files byte-identical (`from . import REPO_ROOT` and all), which
#: is what makes the drift hash comparison meaningful.
VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor"


class MpaBridgeError(ImportError):
    """Main-Profile-Account could not be located or imported."""


def mpa_root() -> Path:
    """The Main-Profile-Account checkout root. Raises if it is not there."""
    env = os.environ.get("MPA_ROOT")
    candidate = Path(env) if env else REPO_ROOT.parent / _DEFAULT_SIBLING
    if not (candidate / "common" / "__init__.py").exists():
        raise MpaBridgeError(
            f"Main-Profile-Account not found at {candidate}. Set MPA_ROOT to its "
            f"checkout root, or place it beside this repo as ../{_DEFAULT_SIBLING}. "
            "Callers that can use the vendored snapshot instead go through "
            "common_kununu_feed(); see leadauto/mpa_bridge.py."
        )
    return candidate


def ensure_on_path() -> Path:
    """Put the checkout root on sys.path (idempotent) and return it."""
    root = mpa_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root


def vendor_files() -> dict[str, Path]:
    """The vendored snapshot's files, keyed by their path within `common/`.

    Used by `common_kununu_feed` to resolve the fallback and by the drift test to
    hash the snapshot against the live checkout.
    """
    root = VENDOR_ROOT / "common"
    return {"__init__.py": root / "__init__.py",
            "kununu_feed.py": root / "kununu_feed.py"}


def _import_common_kununu_feed():
    """Import `common.kununu_feed` from whatever is currently on `sys.path`."""
    from common import kununu_feed  # noqa: PLC0415 - deliberately late
    return kununu_feed


def common_kununu_feed(logger=None):
    """The `common.kununu_feed` module — the ONE feed client for these projects.

    It owns the endpoint URL, the cache location (`KUNUNU_CACHE`), the Basic-auth
    header, and readers that sniff CSV vs JSONL from the file CONTENT rather than
    trusting the extension.

    Resolution order, and the order matters: the real Main-Profile-Account checkout
    first, then the vendored snapshot with a WARNING. Only a missing checkout falls
    back — a checkout that is present but broken raises, because "the reader is
    damaged" and "the reader is elsewhere" call for different fixes and quietly
    running a snapshot instead of a broken canonical module would hide the damage.
    """
    log = logger or get_logger("mpa_bridge")
    try:
        ensure_on_path()
    except MpaBridgeError as absent:
        missing = [str(p) for p in vendor_files().values() if not p.exists()]
        if missing:
            raise MpaBridgeError(
                f"{absent} Additionally the vendored fallback is incomplete "
                f"(missing: {', '.join(missing)})."
            ) from absent
        entry = str(VENDOR_ROOT)
        if entry not in sys.path:
            sys.path.append(entry)   # appended: a real checkout must still win
        try:
            feed = _import_common_kununu_feed()
        except ImportError as err:  # pragma: no cover - surfaced verbatim
            raise MpaBridgeError(
                f"could not import the vendored common.kununu_feed: {err}"
            ) from err
        log.warning(
            "Main-Profile-Account not found (%s); using the vendored snapshot at %s. "
            "It is a copy — see leadauto/_vendor/PROVENANCE.md and re-sync it if the "
            "feed changes.", absent, VENDOR_ROOT,
        )
        return feed
    try:
        return _import_common_kununu_feed()
    except ImportError as err:  # pragma: no cover - surfaced verbatim
        raise MpaBridgeError(f"could not import common.kununu_feed: {err}") from err


def feed_source() -> str:
    """Where the loaded `common.kununu_feed` came from: 'mpa' or 'vendored'.

    Derived from the imported module's own file, never from which branch was taken:
    `sys.modules` is process-wide, so a second call must report what is actually
    loaded. 'unloaded' before anything imported it.
    """
    mod = sys.modules.get("common.kununu_feed")
    if mod is None:
        return "unloaded"
    path = Path(getattr(mod, "__file__", "") or "").resolve()
    return "vendored" if VENDOR_ROOT in path.parents else "mpa"
