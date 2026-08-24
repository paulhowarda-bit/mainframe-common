# cobol-parse

The IBM Enterprise COBOL **parse front-end**: source text in, a typed `Program` AST out.
Pure Python standard library. This is the parser underneath
[`cobol-xstate`](https://github.com/paulhowarda-bit/cobol-xstate-json), packaged on its
own so any program can parse COBOL without carrying the statechart modelling engine.

```python
from cobol_parse import parse_program

prog = parse_program(source_text)
prog.program_id           # PROGRAM-ID
prog.paragraphs           # PROCEDURE DIVISION paragraphs, each a list of statement nodes
prog.data_items           # DATA DIVISION as typed DataItems (PIC/USAGE/OCCURS/REDEFINES)
prog.copybooks            # which COPY members were expanded, and how
```

What it does, in pipeline order:

1. **normalize** — fixed/free source format (auto-detected), column-7 indicator
   handling, continuation-literal stitching, comment stripping.
2. **preprocess** — `COPY` / `COPY … REPLACING` / `EXEC SQL INCLUDE` expansion through a
   pluggable copybook resolver (search paths, extension lists, optional estate fetcher
   from `cobol-xstate-core`), with every expanded line tagged with its copybook origin.
3. **lex** — tokens carrying source line + copybook provenance.
4. **parse** — PROCEDURE DIVISION sections/paragraphs and a control-flow statement AST
   (`IF` / `EVALUATE` / `PERFORM` / `GO TO` / I/O / `CALL` / `ALTER` / handlers /
   `EXEC` blocks), plus the DATA DIVISION as typed `DataItem`s.

Faithfulness rule inherited by every consumer: the parse records what the source says
and where it says it (line + copybook member); what it cannot recover it leaves visibly
unparsed — a whole paragraph that defeats the statement parser is kept as one opaque
action with its `parse_error` recorded, never silently dropped.

Depends only on `cobol-xstate-core` (the estate/artifact-service boundary used for
copybook retrieval). Python ≥ 3.9.
