"""cobol_parse - the IBM Enterprise COBOL parse front-end.

Source text in, a :class:`~cobol_parse.model.Program` out - the typed AST every
consumer (the cobol-xstate statechart compiler first among them) builds on:

    raw source
      -> prefetch     : retrieve the members that COMPLETE the text (COPY / EXEC SQL
                        INCLUDE) through the estate's artifact service, BEFORE parsing -
                        a copybook that does not arrive takes its VALUE clauses out of
                        the model
      -> normalizer   : fixed/free format, column-7 handling, continuation, comments
      -> preprocessor : COPY / REPLACING expansion, EXEC SQL INCLUDE, origin-tagged lines
      -> lexer        : tokens carrying source-line + copybook provenance
      -> parser       : PROCEDURE DIVISION sections/paragraphs + a control-flow
                        statement AST (IF / EVALUATE / PERFORM / GO TO / I/O /
                        terminators / CALL / ALTER), plus the DATA DIVISION as typed
                        DataItems

This distribution carries NO modelling engine: it depends only on cobol-xstate-core
(the estate boundary), and anything downstream - statecharts, views, emitters - lives
in its consumers. Faithfulness rule inherited by every consumer: the parse records
what the source says and where it says it (line + copybook origin); what it cannot
recover it leaves visibly unparsed rather than smoothing over.
"""

import logging as _logging

from .errors import CopybookError, ParseError, SourceFormatError
from .normalizer import (CodeLine, FormatDetection, SourceFormat,
                         detect_source_format, normalize)
from .lexer import Token, tokenize
from .preprocessor import CopybookResolver, PreprocessResult, preprocess, scan_copy_members
from .data_division import DataItem, PicType, expand_pic, parse_data_division, parse_pic
from .model import Paragraph, Program, Stmt, walk_statements
from .parse_bundle import (ParseBundle, ParseBundleError, open_parse_bundle,
                           program_from_dict, program_to_dict, write_parse_bundle)
from .parser import parse_program
from .prefetch import prefetch_cobol
from .textutil import mask_literals

#: This package's top-level logger name. Every module logger (``cobol_parse.parser`` ...)
#: is a child of it, so configuring this one configures the whole package. Applications
#: pass it - alongside core's own root - to
#: ``cobol_xstate_core.logging_setup.configure_logging``; a root that nobody configures
#: either propagates to the root logger or falls back to logging's lastResort and prints
#: straight to stderr.
PACKAGE_LOGGER = "cobol_parse"

# Library logging contract: attach a no-op handler to the package logger so importing
# cobol_parse never emits "No handlers could be found" and never writes to stderr on its
# own. The application decides what to do with these records. Every module logs via
# logging.getLogger(__name__).
_logging.getLogger(PACKAGE_LOGGER).addHandler(_logging.NullHandler())

__all__ = [
    "normalize",
    "CodeLine",
    "FormatDetection",
    "SourceFormat",
    "detect_source_format",
    "tokenize",
    "Token",
    "preprocess",
    "CopybookResolver",
    "PreprocessResult",
    "scan_copy_members",
    "prefetch_cobol",
    "parse_program",
    "Program",
    "Paragraph",
    "Stmt",
    "walk_statements",
    "parse_data_division",
    "parse_pic",
    "expand_pic",
    "DataItem",
    "PicType",
    "mask_literals",
    # The serialized parse contract (see cobol_parse.parse_bundle)
    "ParseBundle",
    "ParseBundleError",
    "open_parse_bundle",
    "write_parse_bundle",
    "program_to_dict",
    "program_from_dict",
    # Error hierarchy (see cobol_parse.errors)
    "SourceFormatError",
    "ParseError",
    "CopybookError",
]

__version__ = "0.1.0"
