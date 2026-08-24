"""Command-line entry point: COBOL file -> parse bundle (serialized ``Program``).

The upfront half of a two-step run. This command retrieves copybooks, parses, and
writes ONE file - the parse bundle - which any consumer (``cobol-xstate --from-parse``
first among them) can model from without re-parsing, and any other program can read as
the machine-checkable record of what the source says.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from cobol_xstate_core.artifact_service import decode_member, load_fetcher
from cobol_xstate_core.bundle import open_bundle
from cobol_xstate_core.cliargs import (add_logging_args, add_retrieval_args,
                                       jobs as _jobs)
from cobol_xstate_core.errors import CobolXstateError
from cobol_xstate_core.logging_setup import PACKAGE_LOGGER as CORE_LOGGER
from cobol_xstate_core.logging_setup import configure_logging

from . import PACKAGE_LOGGER
from .normalizer import SourceFormat, detect_source_format
from .parse_bundle import write_parse_bundle
from .parser import parse_program
from .prefetch import prefetch_cobol
from .preprocessor import CopybookResolver

# Explicit name, NOT __name__: this module is also run as `python -m cobol_parse`,
# where __name__ would put the logger outside configure_logging's reach.
_log = logging.getLogger("cobol_parse.cli")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cobol-parse",
        description="Parse IBM Enterprise COBOL upfront and write a parse bundle - a "
                    "serialized Program AST that cobol-xstate --from-parse (or any "
                    "other consumer) can model from without re-parsing.",
    )
    p.add_argument("source", help="path to a COBOL source file ('-' for stdin)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="where to write the parse bundle "
                        "(default: <source-stem>.parse.json beside the source)")
    p.add_argument("--format", choices=["fixed", "free"],
                   help="source format (default: auto-detect)")
    p.add_argument("-I", "--copybook-path", action="append", default=[],
                   metavar="DIR", help="copybook search directory (repeatable)")
    p.add_argument("--copybook-ext", action="append", default=[], metavar="EXT",
                   help="extra copybook extension to try, e.g. .cpy (repeatable)")
    add_retrieval_args(p)
    p.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    p.add_argument("--diff-producers", action="store_true",
                   help="also run the external Koopa parser (Java, BSD) over the same "
                        "pre-expanded stream and write <output-stem>.parser-diff.json "
                        "- a per-line coverage comparison naming what each parser "
                        "recovered that the other did not. Needs java on PATH and a "
                        "Koopa release jar (--koopa-jar or COBOL_PARSE_KOOPA_JAR).")
    p.add_argument("--koopa-jar", metavar="JAR",
                   help="path to a Koopa release jar "
                        "(https://github.com/krisds/koopa/releases); the jar is never "
                        "bundled. Defaults to $COBOL_PARSE_KOOPA_JAR.")
    add_logging_args(p)
    return p


#: Same default extension chain as the modelling CLI, so the two-step run resolves the
#: same members a one-step run does.
DEFAULT_EXTS = ("", ".cpy", ".CPY", ".cbl", ".cob", ".copy", ".CBL")


def _resolve_format(source: str, name: Optional[str], source_name: str) -> SourceFormat:
    if name is not None:
        return {"fixed": SourceFormat.FIXED, "free": SourceFormat.FREE}[name]
    det = detect_source_format(source)
    level = "detected" if det.is_confident else "WARNING: low-confidence"
    _log.info(f"[{source_name}] {level} source format = {det.format.value} "
              f"({det.confidence:.0%}: {det.reason})")
    if not det.is_confident:
        _log.warning("  -> if the output looks corrupted, re-run with "
                     "--format fixed|free to override.")
    return det.format


def _run(args) -> int:
    if args.gather_only:
        # A gather here would record only stage 1 (this command never builds the
        # artifact manifest that stage 2 plans from), and a later --from-bundle replay
        # RAISES on any member the gather run never asked for - so the half-bundle
        # would poison exactly the run it was made for.
        _log.error("error: --gather-only needs the modelling half (stage 2's plan "
                   "comes from the parsed model); use cobol-xstate --gather-only")
        return 2

    search_paths = list(args.copybook_path)
    if args.source == "-":
        source = sys.stdin.read()
        source_name = "<stdin>"
        out_default = Path("stdin.parse.json")
    else:
        path = Path(args.source)
        if not path.exists():
            _log.error(f"error: no such file: {path}")
            return 2
        source = decode_member(path.read_bytes())
        source_name = path.name
        out_default = path.with_name(path.stem + ".parse.json")
        search_paths.append(str(path.parent))  # look beside the source by default

    fmt = _resolve_format(source, args.format, source_name)

    bundle = None
    if args.from_bundle:
        bundle = open_bundle(args.from_bundle)
        if bundle.source() != source:
            _log.warning(f"[{source_name}] WARNING: this source differs from the one "
                         f"the bundle was gathered for ({bundle.subject_name}); any "
                         f"member it did not ask for is not in the bundle")

    if bundle is not None:
        fetcher, why = bundle.fetcher(), bundle.unavailable
    elif args.no_fetch:
        fetcher, why = None, ("retrieval was disabled for this run, so this member "
                              "was never looked for")
    else:
        fetcher, why = load_fetcher(args.copybook_fetcher)
        if fetcher is None:
            _log.warning(f"[{source_name}] WARNING: {why}")

    all_exts = tuple(args.copybook_ext) + DEFAULT_EXTS
    pre = prefetch_cobol(source, fetcher, paths=search_paths, fmt=fmt,
                         source_name=source_name, unavailable=why,
                         exts=all_exts, jobs=_jobs(args))
    resolver = CopybookResolver(paths=search_paths, exts=all_exts, fetcher=fetcher,
                                store=pre.store)
    program = parse_program(source, fmt, resolver=resolver)

    copybook_errors = tuple(getattr(resolver, "fetch_errors", ()))
    for member, ferr in copybook_errors:
        _log.warning(f"[{source_name}] WARNING: copybook fetcher failed for "
                     f"{member}: {ferr}")

    out = Path(args.output) if args.output else out_default
    written = write_parse_bundle(out, source_name=source_name, source_text=source,
                                 fmt=fmt, program=program,
                                 copybook_errors=copybook_errors, indent=args.indent)
    _log.info(f"[{source_name}] wrote parse bundle {written} "
              f"({len(program.paragraphs)} paragraph(s), "
              f"{len(program.data_items)} data item(s))")
    _log.info(f"[{source_name}] model from it with: cobol-xstate {args.source} "
              f"--from-parse {written}")

    if args.diff_producers:
        import json
        from .normalizer import normalize
        from .preprocessor import preprocess
        from .producers.koopa import diff_producers, run_koopa
        # The same resolver, so Koopa sees exactly the stream the native parse saw
        # (members already resolved and cached; no second retrieval).
        pre_lines = preprocess(normalize(source, fmt), resolver=resolver,
                               fmt=fmt).lines
        report = diff_producers(program, run_koopa(pre_lines, jar=args.koopa_jar),
                                pre_lines)
        base = (out.name[:-len(".parse.json")]
                if out.name.endswith(".parse.json") else out.stem)
        diff_path = out.with_name(base + ".parser-diff.json")
        diff_path.write_text(json.dumps(report, indent=args.indent) + "\n",
                             encoding="utf-8")
        t = report["totals"]
        _log.info(f"[{source_name}] wrote producer diff {diff_path} "
                  f"(native {t['nativeStatements']} vs koopa "
                  f"{t['koopaStatements']} statements; "
                  f"{len(report['parseErrorParagraphs'])} parse-error paragraph(s), "
                  f"{len(report['nativeMissed'])} native-missed line(s), "
                  f"{len(report['koopaMissed'])} koopa-missed line(s))")
    return 0


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose or (1 if args.debug else 0), quiet=args.quiet,
                      loggers=(CORE_LOGGER, PACKAGE_LOGGER))
    try:
        return _run(args)
    except CobolXstateError as exc:
        _log.error("%s", exc)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        if args.debug:
            raise
        _log.critical("internal error while processing %r - re-run with --debug for "
                      "the full traceback", args.source)
        _log.debug("internal error traceback", exc_info=True)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
