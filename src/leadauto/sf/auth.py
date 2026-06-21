"""Salesforce auth resolution.

Local: assume the `sf` CLI is already authenticated to the configured alias (e.g. `headless`).
CI: if SF_AUTH_URL is set, log in non-interactively via the sfdx auth URL and alias it.
One code path downstream: everything targets `--target-org <alias>`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

_ENV = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true"}


def ensure_org(settings, logger=None) -> str:
    alias = settings.org_alias
    auth_url = settings.sf_auth_url
    if auth_url:
        tmp = Path(tempfile.gettempdir()) / "leadauto_sf_auth_url.txt"
        tmp.write_text(auth_url.strip(), encoding="utf-8")
        cmd = (
            f'sf org login sfdx-url --sfdx-url-file "{tmp}" '
            f"--alias {alias} --set-default"
        )
        try:
            res = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, env=_ENV
            )
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        if res.returncode != 0:
            raise RuntimeError(f"sfdx-url login failed:\n{res.stderr}")
        if logger:
            logger.info("Authenticated via sfdx auth URL (alias=%s)", alias)
    elif logger:
        logger.info("Using pre-authenticated org alias=%s", alias)
    return alias
