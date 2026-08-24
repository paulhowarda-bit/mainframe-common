"""Stage 1 for COBOL: close over the COPY / EXEC SQL INCLUDE members the program needs.

**Discovered lexically.** ``COPY X.`` names its member right there in the text, so no
parse is needed and this can genuinely run BEFORE parsing - which is the whole point,
because the parse is what produces the dependency manifest that stage 2 works from. A
copybook that does not arrive takes its ``VALUE`` clauses out of the model, which turns a
resolvable dynamic ``CALL`` target into an unresolved runtime name, and the program it
calls then never becomes a row to fetch at all. Nothing errors; the answer is just
quietly short, which is the worst possible failure for an impact analysis.

The engine underneath - the local-before-network rule, the request planning, and the
report that keeps "the estate was asked and had nothing" apart from "we could not ask" -
is shared with the JCL closure and lives in :mod:`cobol_xstate_core.prefetch`.
"""

from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Tuple

from cobol_xstate_core.prefetch import (PrefetchResult, Prefetcher,  # noqa: F401
                                        member_key)

from .normalizer import SourceFormat
from .preprocessor import scan_copy_members


def prefetch_cobol(source: str, fetcher: Optional[Callable],
                   paths: Optional[List[str]] = None, dest: Optional[str] = None,
                   fmt: Optional[SourceFormat] = None,
                   source_name: str = "<source>",
                   unavailable: Optional[str] = None,
                   result: Optional[PrefetchResult] = None,
                   exts: Optional[Tuple[str, ...]] = None,
                   jobs: int = 1,
                   seen: Optional[Iterable[str]] = None) -> PrefetchResult:
    """Close over every ``COPY`` / ``EXEC SQL INCLUDE`` member the program needs.

    Transitive: each retrieved member is scanned in turn, because a copybook that COPYs
    another copybook has a hole in it exactly like the program did. Cycles terminate on
    the seen-set, so a mutually-including pair costs one fetch each."""
    pf = Prefetcher(fetcher, paths, dest, unavailable, result, exts, seen=seen)
    pf.name_source(source_name)

    # Level by level, not member by member. The worklist GROWS as members are read - a
    # copybook names its own COPYs only in its text - so the members one level down are
    # not knowable until this level has arrived. That makes the level, and only the
    # level, the thing that can be retrieved together.
    wave: List[Tuple[str, str]] = [(m, "COPY in the program") for m in
                                   scan_copy_members(source, fmt)]
    while wave:
        nxt: List[Tuple[str, str]] = []
        for member, text in pf.obtain_wave(wave, "copybook", jobs):
            for nested in scan_copy_members(text, fmt):
                if member_key(nested) not in pf.seen:
                    nxt.append((nested, f"COPY inside {member}"))
        wave = nxt
    return pf.result
