"""Argument groups both CLIs share, so they cannot drift apart.

``--jobs`` meaning one thing to the COBOL command and another to the JCL one would be a
small bug with a long tail: the two write reports that are meant to be read together.
Defining the shared flags once is cheaper than discovering that later.

Only genuinely shared flags live here. Anything that is one front-end's own business -
``--target``, ``--bind-jcl``, ``--max-rounds`` - stays with its own parser.
"""

from __future__ import annotations

import argparse

from .artifact_service import DEFAULT_FETCHER


def add_logging_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="increase log detail: -v adds DEBUG (swallowed tracebacks and "
                        "internal steps). Diagnostics go to stderr; stdout is unaffected.")
    p.add_argument("-q", "--quiet", action="count", default=0,
                   help="reduce log detail: -q shows only warnings and errors (hides "
                        "progress), -qq shows only errors.")
    p.add_argument("--debug", action="store_true",
                   help="on an unexpected internal error, print the full Python traceback "
                        "instead of a one-line message (for bug reports). Implies -v.")


def add_retrieval_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--copybook-fetcher", "--fetcher", dest="copybook_fetcher",
                   metavar="MODULE:FUNC",
                   help=f"override the estate artifact service. Every run retrieves its "
                        f"dependencies through {DEFAULT_FETCHER} by default - only the "
                        f"estate knows where its members live - so this is needed only "
                        f"for a differently-named client. FUNC(name, type=, copy=) may "
                        f"return the member text, (text, source), or a dict with "
                        f"text/path/copied_to/detected_type/alternatives")
    p.add_argument("--jobs", type=int, default=8, metavar="N",
                   help="how many members to request from the estate service at once "
                        "(default: 8). Retrieval is most of a run's wall clock and the "
                        "requests do not depend on each other, so they overlap. The "
                        "reports are byte-identical at any N - row order follows the "
                        "plan, never the order answers arrive. Use --jobs 1 for a "
                        "strictly sequential run (no threads at all) if your estate "
                        "client is not thread-safe or you must not load it up.")
    p.add_argument("--gather-only", metavar="DIR",
                   help="run only the retrieval half and write a replayable ESTATE "
                        "BUNDLE to DIR: the source, every member that came off the "
                        "estate, and a record of every answer - including the misses, "
                        "which decide what a later report says. No view is written. Use "
                        "this on the machine that can reach the estate, then --from-bundle "
                        "on the one that models.")
    p.add_argument("--from-bundle", metavar="DIR",
                   help="model from an estate bundle written by --gather-only, with no "
                        "network at all. Retrieval runs exactly as it would live; the "
                        "bundle simply answers instead of the estate, so the reports say "
                        "what the gathering run's said. Asking for something the gather "
                        "run never asked for is an error, not an empty answer.")
    p.add_argument("--no-fetch", action="store_true",
                   help="do not contact the estate at all. Members already on the "
                        "copybook search path still resolve; everything else is reported "
                        "as deliberately not looked for - which is NOT the same as the "
                        "estate having nothing, and is not reported as if it were.")


def add_output_args(p: argparse.ArgumentParser, *, outdir_help: str) -> None:
    p.add_argument("--outdir", default="out", metavar="DIR", help=outdir_help)
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--summary", action="store_true",
                   help="print a human-readable summary to stderr")
    p.add_argument("--timing", action="store_true",
                   help="print per-stage wall-clock timings to stderr (diagnostic; "
                        "does not affect any output file)")


def jobs(args) -> int:
    """How many members may be in flight at once, floored at 1.

    Clamped rather than rejected: ``--jobs 0`` means "do not overlap", which is a coherent
    thing to ask for and is what 1 does. It must reach EVERY retrieval call site in both
    run paths - a flag that got as far as one stage and not the other would leave half a
    run sequential while the help text said otherwise, which is how --copybook-ext was
    wrong before it.
    """
    return max(1, int(getattr(args, "jobs", 1) or 1))
