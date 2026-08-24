"""The serialized parse contract: a ``Program`` as a replayable JSON *parse bundle*.

Run the parse UPFRONT - on another machine, in another process, from another program -
and feed the result to any consumer without re-parsing. The design rules are the estate
bundle's (:mod:`cobol_xstate_core.bundle`), because they earned their keep there:

* **Replay introduces no new branch.** A consumer that accepts a parse bundle skips
  exactly one call (``parse_program``) and continues with a real, rehydrated
  :class:`~cobol_parse.model.Program`; everything downstream is the same code, byte for
  byte. The proof lives in the byte-stability gate: every view of every example is
  hashed both direct and through a serialize/deserialize round trip, against the SAME
  goldens.
* **No timestamps anywhere.** Parsing the same source twice produces identical bytes;
  evidence that changes when nothing changed is impossible to diff.
* **Staleness is a hard error.** The bundle records the sha256 of the exact source text
  it parsed. A consumer given different text must refuse: a stale ``Program`` is not a
  degraded answer, it is a silently WRONG one - every provenance line number and every
  recovered statement would describe a file that no longer says that.
* **A newer format is refused, never partially read** - upgrade the tool instead.

Encoding: every dataclass instance becomes ``{"t": "<ClassName>", <field>: <value>...}``
with JSON keys exactly the dataclass field names, in declaration order. The decoder
rebuilds REAL dataclass instances - consumers dispatch on ``isinstance`` and read
attributes, so a dict-shaped impostor would not do. Two representational details are
restored on read: tuple-typed fields (JSON has only arrays), and
``Program.data_by_name``, stored as ``{name: index-into-data_items}`` so the rehydrated
mapping holds the SAME objects as ``data_items`` (downstream may reach an item through
either and must see one item, not two copies).

The contract is versioned against :mod:`cobol_parse.model`: any change to a field set
there is a ``VERSION`` bump here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .data_division import DataItem, PicType
from .errors import ParseError
from .model import (Action, AlterStmt, CallStmt, ContinueStmt, EvaluateStmt,
                    ExecStmt, ExitStmt, GoToStmt, HandledStmt, IfStmt, IoStmt,
                    Paragraph, PerformStmt, Program, SearchStmt, SortStmt,
                    TerminateStmt)
from .normalizer import SourceFormat

FORMAT = "cobol-parse"
# Version history (a bump per model field-set change; older versions still open, the
# missing fields taking their dataclass defaults - a NEWER version is refused):
#   1  the initial contract
#   2  Program.sql_cursors / Program.declared_tables (whole-stream SQL declaration
#      scan) and ExecStmt.table / ExecStmt.values_list (column-list-less INSERT)
VERSION = 2
#: The producer name this package writes. An external producer (a different parser
#: emitting the same contract) writes its own, so a reader can tell whose parse it is.
PRODUCER = "cobol-parse-python"


class ParseBundleError(ParseError):
    """A parse bundle could not be written, read, or trusted (wrong format, newer
    version, unknown node type, or source text that no longer matches the recorded
    hash)."""


# Every dataclass the encoder may meet, keyed by the type tag it writes. Paragraph and
# Program ride along with the statement nodes; DataItem carries a nested PicType.
_CLASSES = {cls.__name__: cls for cls in (
    Action, AlterStmt, CallStmt, ContinueStmt, EvaluateStmt, ExecStmt, ExitStmt,
    GoToStmt, HandledStmt, IfStmt, IoStmt, Paragraph, PerformStmt, SearchStmt,
    SortStmt, TerminateStmt, DataItem, PicType,
)}

# Fields declared as tuples in the model, restored from JSON arrays on read. An explicit
# table rather than type-hint reflection: the hints are strings under
# `from __future__ import annotations`, and resolving them on 3.9 is more machinery than
# four facts deserve.
#   EvaluateStmt.whens / SearchStmt.whens : List[Tuple[str, List[Stmt]]]
#   AlterStmt.pairs                       : List[Tuple[str, str]]
#   Program.cics_handlers                 : List[tuple]  (condition, target)
_PAIR_LIST_FIELDS = {
    ("EvaluateStmt", "whens"),
    ("SearchStmt", "whens"),
    ("AlterStmt", "pairs"),
    ("Program", "cics_handlers"),
}


def _encode(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj):
        name = type(obj).__name__
        if name not in _CLASSES:
            raise ParseBundleError(
                f"cannot serialize a {name!r} node: it is not part of parse-bundle "
                f"format {FORMAT} v{VERSION} (a model change needs a version bump here)")
        out = {"t": name}
        for f in dataclasses.fields(obj):
            out[f.name] = _encode(getattr(obj, f.name))
        return out
    raise ParseBundleError(
        f"cannot serialize a value of type {type(obj).__name__!r} into a parse bundle")


def _decode(obj):
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    if isinstance(obj, dict):
        tag = obj.get("t")
        if tag is None:
            return {k: _decode(v) for k, v in obj.items()}
        cls = _CLASSES.get(tag)
        if cls is None:
            raise ParseBundleError(
                f"unknown node type {tag!r} in parse bundle - written by a newer or "
                f"different producer; upgrade this tool rather than reading it partially")
        kwargs = {k: _decode(v) for k, v in obj.items() if k != "t"}
        for key in list(kwargs):
            if (tag, key) in _PAIR_LIST_FIELDS:
                kwargs[key] = [tuple(pair) for pair in kwargs[key]]
        try:
            return cls(**kwargs)
        except TypeError as exc:
            raise ParseBundleError(
                f"a {tag} node in this parse bundle does not match this tool's model "
                f"({exc}) - the bundle was written by a different version") from None
    return obj


def program_to_dict(program: Program) -> dict:
    """The ``Program`` as one JSON-ready dict, fields in declaration order.

    ``data_by_name`` is stored as ``{name: index-into-data_items}`` - the mapping's whole
    point is that its values ARE items of ``data_items``, and serializing the objects
    twice would quietly turn one item into two on the way back in."""
    index = {id(item): i for i, item in enumerate(program.data_items)}
    out = {"t": "Program"}
    for f in dataclasses.fields(Program):
        if f.name == "data_by_name":
            by: Dict[str, int] = {}
            for name, item in program.data_by_name.items():
                i = index.get(id(item))
                if i is None:
                    raise ParseBundleError(
                        f"data_by_name[{name!r}] holds an object that is not in "
                        f"data_items; the parse bundle cannot preserve its identity")
                by[name] = i
            out[f.name] = by
        else:
            out[f.name] = _encode(getattr(program, f.name))
    return out


def program_from_dict(data: dict) -> Program:
    """Rebuild a real :class:`Program` (real dataclass instances all the way down)."""
    if not isinstance(data, dict) or data.get("t") != "Program":
        raise ParseBundleError("not a serialized Program (missing the Program type tag)")
    kwargs = {}
    for k, v in data.items():
        if k in ("t", "data_by_name"):
            continue
        if ("Program", k) in _PAIR_LIST_FIELDS:
            kwargs[k] = [tuple(pair) for pair in v]
        else:
            kwargs[k] = _decode(v)
    try:
        program = Program(**kwargs)
    except TypeError as exc:
        raise ParseBundleError(
            f"this parse bundle's Program does not match this tool's model ({exc}) - "
            f"the bundle was written by a different version") from None
    by_name = data.get("data_by_name", {})
    try:
        program.data_by_name = {name: program.data_items[i]
                                for name, i in by_name.items()}
    except (IndexError, TypeError):
        raise ParseBundleError("data_by_name indexes an item that data_items does not "
                               "have - the bundle is corrupt") from None
    return program


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ParseBundle:
    """An opened parse bundle: the recorded source identity plus the Program.

    ``program()`` decodes fresh on every call, so a consumer that mutates the returned
    object (several analyses do) cannot poison a later replay from the same bundle."""

    def __init__(self, doc: dict, path: str):
        self._doc = doc
        self.path = path
        src = doc["source"]
        self.source_name: str = src["name"]
        self.source_text: str = src["text"]
        self.sha256: str = src["sha256"]
        self.fmt: SourceFormat = SourceFormat(src["format"])
        self.producer: str = doc.get("producer", "")
        self.copybook_errors: Tuple[Tuple[str, str], ...] = tuple(
            (m, e) for m, e in doc.get("copybookErrors", []))

    def program(self) -> Program:
        return program_from_dict(self._doc["program"])

    def check_source(self, source_text: str, source_name: str = "<source>") -> None:
        """Refuse a source that is not the one this bundle parsed.

        A HARD error on purpose, unlike the estate bundle's drift warning: an estate
        answer for changed source is merely incomplete, but a Program for changed source
        is wrong everywhere at once - lines, statements, provenance - with nothing left
        to notice it."""
        if _sha256(source_text) != self.sha256:
            raise ParseBundleError(
                f"{source_name} is not the source this parse bundle was produced from "
                f"({self.source_name}); re-run the producer rather than modelling a "
                f"stale parse")


def write_parse_bundle(path, *, source_name: str, source_text: str,
                       fmt: SourceFormat, program: Program,
                       copybook_errors: Sequence[Tuple[str, str]] = (),
                       producer: str = PRODUCER, indent: int = 2) -> str:
    """Write one parse bundle. Returns the path written."""
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "producer": producer,
        "source": {
            "name": source_name,
            "format": fmt.value,
            "sha256": _sha256(source_text),
            "text": source_text,
        },
        "copybookErrors": [[m, e] for m, e in copybook_errors],
        "program": program_to_dict(program),
    }
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=indent) + "\n", encoding="utf-8")
    return str(p)


def open_parse_bundle(path) -> ParseBundle:
    """Open and validate a parse bundle; every refusal names its reason."""
    p = Path(path)
    if not p.is_file():
        raise ParseBundleError(f"no such parse bundle: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParseBundleError(f"cannot read parse bundle {p}: {exc}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ParseBundleError(f"{p} is not a parse bundle (format != {FORMAT!r})")
    version = doc.get("version")
    if not isinstance(version, int) or version > VERSION:
        raise ParseBundleError(
            f"{p} is parse-bundle version {version}, newer than this tool understands "
            f"(<= {VERSION}); upgrade the tool rather than reading it partially")
    for key in ("source", "program"):
        if key not in doc:
            raise ParseBundleError(f"{p} is missing its {key!r} record - the bundle is "
                                   f"incomplete")
    src = doc["source"]
    for key in ("name", "format", "sha256", "text"):
        if key not in src:
            raise ParseBundleError(f"{p} source record is missing {key!r}")
    try:
        SourceFormat(src["format"])
    except ValueError:
        raise ParseBundleError(f"{p} records an unknown source format "
                               f"{src['format']!r}") from None
    return ParseBundle(doc, str(p))
