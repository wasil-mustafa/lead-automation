# Vendored snapshot: `common/` from Main-Profile-Account

| | |
|---|---|
| Source repo | `Nicolaus-Pichorner/Main-Profile-Account` (private) |
| Source path | `common/__init__.py`, `common/kununu_feed.py` |
| Snapshot taken | 2026-09-18 (re-synced; first snapshot 2026-08-19) |
| Source repo HEAD | `a6b66d78cbb01b7c1ec979f0a717a7133d3f27a8` |
| Last commit touching `kununu_feed.py` | `9f9bd502aa8bba508a736c2c8b9d895c03245850` — *feat(mislink): flag accounts whose name contradicts their linked profile* (2026-08-28) |

sha256 at snapshot time:

```
e09a2496ed0502aee6ecd9999e8f615cc332836b7ba7d795c3f099f22f935982  common/__init__.py
8846e1c11f9faf9bb5cf94f4b3d305b20bb5b4b1d3b0c28cdcc6f1526e2bdb00  common/kununu_feed.py
```

## Why a copy exists at all

CI needs the feed reader, the reader lives in another private repo, and the
`GITHUB_TOKEN` GitHub Actions injects cannot read another repository. See
`__init__.py` for the full rationale and the guards.

Only two files are copied because `common/kununu_feed.py` imports nothing from the
rest of the package except `REPO_ROOT` (`from . import REPO_ROOT`) — everything else
it uses is stdlib. If a future version imports another `common` module, the drift test
fails on the hash and the new dependency must be vendored with it.

## Re-syncing

Copy the files over and update the table above:

```bash
MPA=../Main-Profile-Account
cp "$MPA/common/__init__.py"    src/leadauto/_vendor/common/__init__.py
cp "$MPA/common/kununu_feed.py" src/leadauto/_vendor/common/kununu_feed.py
sha256sum src/leadauto/_vendor/common/*.py
git -C "$MPA" rev-parse HEAD
python -m pytest tests/test_mpa_vendor.py -q
```

Do NOT edit the copies. A fix belongs upstream in Main-Profile-Account, which owns
this code; the snapshot then follows it. `tests/test_mpa_vendor.py` fails on any
difference whenever both checkouts are present, which is how a local edit or an
upstream change gets noticed.

## Known residual risk

The drift test can only compare where Main-Profile-Account is checked out — i.e. not
on a CI runner. If upstream changes and nobody runs the suite locally before the next
scheduled run, CI keeps executing this snapshot. That window is the price of not
holding a cross-repo credential; `KUNUNU_FEED_URL` remains the escape hatch for an
endpoint change (`common.kununu_feed` reads it at call time), so a feed move can be
worked around by config while the snapshot catches up.
