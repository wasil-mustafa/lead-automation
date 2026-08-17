"""`sys.path` bridge to the Main-Profile-Account checkout, so this repo can import
its shared, domain-free infrastructure package:

    from leadauto.mpa_bridge import common_kununu_feed
    feed = common_kununu_feed()          # the `common.kununu_feed` module

Path resolution: the `MPA_ROOT` env var, else the sibling directory
`../Main-Profile-Account`. Derived, never hardcoded to a user profile — the two
laptops that work these projects have different profile names.

FAIL LOUDLY, never fall back to a local copy. A second private feed parser is
exactly how this project ended up requesting a dead `.jsonl` endpoint for eleven
days after kununu switched the feed to CSV on 2026-08-06, degrading to "no
profiles" with nothing but a WARNING — while Main-Profile-Account, which owns the
canonical reader, followed the switchover the same day. `common/kununu_feed.py`
documents that failure mode in its own module docstring; this bridge exists so it
cannot happen here a second time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .state import REPO_ROOT

_DEFAULT_SIBLING = "Main-Profile-Account"


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
            "This project deliberately keeps no local copy of common/ — see "
            "leadauto/mpa_bridge.py."
        )
    return candidate


def ensure_on_path() -> Path:
    """Put the checkout root on sys.path (idempotent) and return it."""
    root = mpa_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root


def common_kununu_feed():
    """The `common.kununu_feed` module — the ONE feed client for these projects.

    It owns the endpoint URL, the cache location (`KUNUNU_CACHE`), the Basic-auth
    header, and readers that sniff CSV vs JSONL from the file CONTENT rather than
    trusting the extension.
    """
    ensure_on_path()
    try:
        from common import kununu_feed  # noqa: PLC0415 - deliberately late
    except ImportError as err:  # pragma: no cover - surfaced verbatim
        raise MpaBridgeError(f"could not import common.kununu_feed: {err}") from err
    return kununu_feed
