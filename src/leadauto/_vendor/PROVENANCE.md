# Vendored snapshot: `common/` from Main-Profile-Account

| | |
|---|---|
| Source repo | `Nicolaus-Pichorner/Main-Profile-Account` (private) |
| Source path | `common/__init__.py`, `common/kununu_feed.py` |
| Snapshot taken | 2026-08-19 |
| Source repo HEAD | `8bd40190160b86e755464f5d4d436bdcd67a16ce` |
| Last commit touching `kununu_feed.py` | `4824f0935f7703f9055e8d1a01be50a9c5f40f12` — *feat(feed): read the CSV profiles feed in every consumer, not just the tagger* (2026-08-06) |

sha256 at snapshot time:

```
e09a2496ed0502aee6ecd9999e8f615cc332836b7ba7d795c3f099f22f935982  common/__init__.py
1fecadd6b733a9e1d6fba5d555f6f36d9c48cdad80685d599002c28170238956  common/kununu_feed.py
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
