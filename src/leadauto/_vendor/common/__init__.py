"""Shared infrastructure for the Main Profile tagger and its sibling scripts.

Everything in here is DOMAIN-FREE: the Salesforce transport, path/CSV helpers,
company-name normalization, and the kununu feed client. Nothing in this package
knows about the Main Profile tag, the contamination rules, or the tiebreaker
cascade - that all stays in `main_profile_tag.py`. Consequently `common.*` must
never import `main_profile_tag` (the same circular-import rule `sf_bulk` has
always had).

Why it exists: nine sibling scripts were importing `run_bulk_query`, `norm_name`,
`write_lf_csv` and friends FROM the 2,770-line tagger, which meant pulling in
pandas and the whole cascade just to write a CSV. They now import from here.
`main_profile_tag` re-exports the same names, so no caller broke.

REPO_ROOT exists because three defaults used to be derived from
`main_profile_tag.py`'s own `__file__`: the `log/` output dir, the kununu feed
cache, and the Bulk API read cache. Moving that code one directory down into this
package would have silently relocated all three - the log dir to `common/log`
(breaking the CI workflow's artifact globs, which expect the CSVs next to the
script on the Linux runner), and both caches to `common/`. Deriving them from
REPO_ROOT keeps every path exactly where it was.
"""
from pathlib import Path

# The checkout root - one level above this package.
REPO_ROOT = Path(__file__).resolve().parents[1]
