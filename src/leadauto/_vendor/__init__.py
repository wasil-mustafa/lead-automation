"""A byte-identical SNAPSHOT of `common/` from the Main-Profile-Account repo.

Nothing in here is edited, ever. It exists so CI can read the kununu profile feed
without a credential for a second private repo: `common.kununu_feed` owns the feed
endpoint, the Basic-auth header and the CSV/JSONL content sniffing, it lives in
`Nicolaus-Pichorner/Main-Profile-Account`, and the `GITHUB_TOKEN` Actions injects is
scoped to THIS repository only. The alternatives were a classic PAT (read on every
repo the owner can see) or a deploy key on a repo owned by someone else; Wasil chose
a vendored snapshot on 2026-08-19.

This deliberately relaxes the standing "import it, never vendor it" rule, so the
guards that make it survivable are not optional:

  * `mpa_bridge.common_kununu_feed()` still prefers the REAL checkout and only falls
    back here, with a warning naming this snapshot. Wherever Main-Profile-Account is
    present - every local run, and CI if `MPA_PAT` is ever set - the canonical module
    is what executes, so the snapshot is a CI-only backstop rather than a second
    implementation racing the first.
  * `tests/test_mpa_vendor.py` hashes these files against the live ones and FAILS on
    any difference. It runs on the machines that have both checkouts (where the suite
    is actually run), turning drift into a red test instead of the silent wrong answer
    a fork usually produces.
  * `PROVENANCE.md` records the commit this came from and the command to re-sync.

The failure this is guarding against is documented, not hypothetical: kununu moved
the feed from JSONL to CSV on 2026-08-06, this repo's own private reader kept asking
for `profiles.jsonl`, and because every failure path returned `{}` the pipeline ran
eleven days with no alt names and no `profile_score` - which fails the live-scope gate
closed, so nothing could convert. `common` had followed the switchover the same day.
A snapshot that is never checked becomes exactly that reader again.
"""
