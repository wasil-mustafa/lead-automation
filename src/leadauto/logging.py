"""Lightweight structured logging for the pipeline (console; JSON-friendly in CI)."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "leadauto", level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s")
        )
        root = logging.getLogger("leadauto")
        root.addHandler(handler)
        root.setLevel(level)
        root.propagate = False
        _CONFIGURED = True
    return logging.getLogger(name if name.startswith("leadauto") else f"leadauto.{name}")
