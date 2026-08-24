"""The per-run retrieval account both CLIs print, worded once.

The holes get NAMED rather than counted because every downstream view is read as if it
were complete. A member that did not arrive is the reason a dynamic CALL stayed
unresolved or a job looks like it has fewer steps than it runs, and a reader who cannot
see which member that was has no way to tell an accurate model from a short one.
"""

from __future__ import annotations

import logging
from typing import Optional


def report_stages(log: logging.Logger, source_name: str, pre, fetched: dict) -> None:
    """One line per retrieval stage on stderr, plus every hole named individually."""
    pc, fc = pre.counts, (fetched or {}).get("counts", {})
    log.info(f"[{source_name}] prefetch: "
             f"{pc.get('fetched', 0)} fetched, {pc.get('local', 0)} local, "
             f"{pc.get('not-found', 0)} not-found, {pc.get('error', 0)} error"
             + (f", {pc.get('no-service', 0)} never looked for"
                if pc.get("no-service") else ""))
    log.info(f"[{source_name}] fetch: "
             f"{fc.get('fetched', 0)} fetched, {fc.get('prefetched', 0)} already in hand, "
             f"{fc.get('not-found', 0)} not-found, {fc.get('error', 0)} error, "
             f"{fc.get('skipped', 0)} not fetchable")
    for member in pre.missing:
        log.warning(f"  MISSING {member}: the source text is incomplete without it - data "
                    f"items or steps it defines are NOT in the model")
    for err in (fetched or {}).get("errors", []):
        log.warning(f"  FETCH ERROR {err['artifact']}: {err['error']}")
