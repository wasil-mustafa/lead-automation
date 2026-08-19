"""The vendored `common/` snapshot: drift against upstream, and the fallback path.

The snapshot exists so CI can read the kununu feed without a credential for the private
Main-Profile-Account repo (see `leadauto/_vendor/__init__.py`). A copy nobody checks is
how this project once spent eleven days asking a dead endpoint for the feed, so the
drift test below is the price of keeping it.
"""
import hashlib
import sys
from pathlib import Path

import pytest

from leadauto import kununu_feed as leadauto_feed
from leadauto.mpa_bridge import (VENDOR_ROOT, MpaBridgeError, common_kununu_feed,
                                 feed_source, mpa_root, vendor_files)


def _digest(path):
    """sha256 of the file with line endings normalised.

    Both checkouts sit in git repos on Windows, so the working copies can differ in
    CRLF alone. Hashing raw bytes would fail on that and teach everyone to ignore this
    test — which would defeat the point of having it.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def isolated_common(monkeypatch):
    """Unload any imported `common` and sandbox `sys.path`.

    `sys.modules` is process-wide: whichever `common` a previous test imported would
    otherwise decide the answer here, and a `sys.path` entry appended by the fallback
    would leak into every test that runs after it.

    Entries that already provide `common/` are dropped too. The fallback APPENDS the
    vendor root precisely so a real checkout keeps winning, so a leftover `MPA_ROOT`
    entry from an earlier `ensure_on_path()` would import the canonical module and the
    fallback assertion would pass for the wrong reason.
    """
    for name in [k for k in sys.modules if k == "common" or k.startswith("common.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    clean = [p for p in sys.path
             if p and not (Path(p) / "common" / "__init__.py").exists()]
    monkeypatch.setattr(sys, "path", clean)


# --- drift -------------------------------------------------------------------

def test_vendored_snapshot_matches_the_live_common():
    """The snapshot must be byte-identical to the code it was copied from.

    Skipped where Main-Profile-Account is not checked out (CI runners), which is the
    documented residual risk: the check only fires on the machines that hold both
    repos, and those are where the suite actually runs.
    """
    try:
        live = mpa_root() / "common"
    except MpaBridgeError as err:
        pytest.skip(f"Main-Profile-Account not available: {err}")
    for name, vendored in vendor_files().items():
        assert _digest(vendored) == _digest(live / name), (
            f"leadauto/_vendor/common/{name} has drifted from {live / name}. "
            "Upstream owns this code: re-sync the snapshot per "
            "leadauto/_vendor/PROVENANCE.md rather than editing the copy."
        )


def test_snapshot_carries_the_reader_api_the_pipeline_calls():
    """Import the snapshot on its own (as `leadauto._vendor.common`) and check the three
    entry points `leadauto.kununu_feed` uses. Importing it by package path rather than
    as `common` keeps this independent of which copy the bridge resolved."""
    from leadauto._vendor.common import kununu_feed as snapshot
    for attr in ("iter_feed_records", "download_feed", "_kununu_cache_path"):
        assert callable(getattr(snapshot, attr)), attr


def test_snapshot_needs_nothing_from_common_beyond_repo_root():
    """Only two files are vendored, which is only safe while `kununu_feed` imports
    nothing else from its package. A new intra-package import must be vendored with it,
    so pin the assumption here as well as in PROVENANCE.md."""
    src = (VENDOR_ROOT / "common" / "kununu_feed.py").read_text(encoding="utf-8")
    relative = [ln.strip() for ln in src.splitlines()
                if ln.startswith("from .") or ln.startswith("from common")]
    assert relative == ["from . import REPO_ROOT"], relative


# --- resolution order --------------------------------------------------------

def test_real_checkout_wins_when_present(isolated_common):
    try:
        mpa_root()
    except MpaBridgeError as err:
        pytest.skip(f"Main-Profile-Account not available: {err}")
    common_kununu_feed()
    assert feed_source() == "mpa"


def test_falls_back_to_the_snapshot_when_the_checkout_is_absent(isolated_common, monkeypatch, caplog):
    def _absent():
        raise MpaBridgeError("Main-Profile-Account not found at /nowhere")
    monkeypatch.setattr("leadauto.mpa_bridge.ensure_on_path", _absent)
    with caplog.at_level("WARNING"):
        mod = common_kununu_feed()
    assert feed_source() == "vendored"
    assert VENDOR_ROOT in Path(mod.__file__).resolve().parents
    assert callable(mod.iter_feed_records)
    # The fallback must never be silent: that is the whole difference between this and
    # the private reader that failed for eleven days behind one warning.
    assert any("vendored snapshot" in r.getMessage() for r in caplog.records)


def test_broken_checkout_raises_instead_of_falling_back(isolated_common, monkeypatch, tmp_path):
    """A checkout that is PRESENT but unimportable is a different fault from a missing
    one and must surface. Silently running the snapshot instead would hide the damage."""
    monkeypatch.setattr("leadauto.mpa_bridge.ensure_on_path", lambda: tmp_path)
    monkeypatch.setattr("leadauto.mpa_bridge._import_common_kununu_feed",
                        lambda: (_ for _ in ()).throw(ImportError("no module named common")))
    with pytest.raises(MpaBridgeError):
        common_kununu_feed()


# --- the 200 MB trap ---------------------------------------------------------

def test_vendored_cache_path_stays_out_of_the_checkout(monkeypatch, tmp_path):
    """On the snapshot, `common`'s default cache anchors on its own REPO_ROOT — which is
    `leadauto/_vendor/`, i.e. ~200 MB inside the OneDrive-synced checkout. The filename
    still comes from `common`; only the directory is replaced."""
    monkeypatch.delenv("KUNUNU_CACHE", raising=False)
    monkeypatch.setattr("leadauto.kununu_feed.feed_source", lambda: "vendored")
    monkeypatch.setattr("leadauto.kununu_feed.common_kununu_feed",
                        lambda: type("M", (), {"_kununu_cache_path":
                                               staticmethod(lambda: str(VENDOR_ROOT / "profiles.csv"))}))
    settings = type("S", (), {"kununu_feed_cache_path": None})()
    path = leadauto_feed.feed_cache_path(settings)
    assert path.name == "profiles.csv"
    assert VENDOR_ROOT not in path.parents
    assert path.parent.name == "kununu-feed"


def test_explicit_kununu_cache_is_honoured_on_the_snapshot(monkeypatch, tmp_path):
    """CI sets KUNUNU_CACHE, and that must win over the temp-dir redirect above."""
    target = tmp_path / "runner" / "profiles.csv"
    monkeypatch.setenv("KUNUNU_CACHE", str(target))
    monkeypatch.setattr("leadauto.kununu_feed.feed_source", lambda: "vendored")
    monkeypatch.setattr("leadauto.kununu_feed.common_kununu_feed",
                        lambda: type("M", (), {"_kununu_cache_path":
                                               staticmethod(lambda: str(target))}))
    settings = type("S", (), {"kununu_feed_cache_path": None})()
    assert leadauto_feed.feed_cache_path(settings) == target
