# mainframe-common

The shared foundation of the mainframe-modernization toolset: **one repository shipping
two distributions**, each with its own `pyproject.toml`. Pure Python standard library,
no runtime dependencies, Python ≥ 3.9.

| Directory | Distribution | What it is | Depends on |
|---|---|---|---|
| `core/` | `cobol-xstate-core` | the estate boundary: the artifact-service protocol, two-stage dependency retrieval, the replayable estate bundle | nothing |
| `parser/` | `cobol-parse` | the IBM Enterprise COBOL parse front-end: source → `Program` AST (normalize / preprocess COPY/REPLACING / lex / parse / data division), `cobol-parse` CLI → parse bundle | core |

The consumers live in their own repositories, and this repo knows nothing about them:

- [`cobol-xstate-json`](https://github.com/paulhowarda-bit/cobol-xstate-json) —
  `cobol-xstate`, the `Program` → XState v5 statechart modelling engine (depends on
  **both** distributions here).
- [`jcl-dependencies`](https://github.com/paulhowarda-bit/jcl-dependencies) — the JCL
  front-end (depends on **core only**). The two front-ends are peers: neither imports
  the other.

The dependency arrows point one way — `cobol_parse` imports `cobol_xstate_core`, and
nothing here imports a modelling engine. A third-party parser consumer gets `source in,
Program out` and carries nothing else; the venv proof lives with the consumer
(`tools/prove_separation.py` in cobol-xstate-json).

## Install

```bash
python -m pip install -e core -e parser        # from a checkout
```

Or, until the distributions are on an index, straight from the repo — both in one
command, so pip resolves `cobol-parse`'s dependency on core from the explicit reference
rather than looking for it on an index:

```bash
pip install "cobol-xstate-core @ git+https://github.com/paulhowarda-bit/mainframe-common#subdirectory=core" "cobol-parse @ git+https://github.com/paulhowarda-bit/mainframe-common#subdirectory=parser"
```

## Use

```bash
cobol-parse prog.cbl -o prog.parse.json    # source -> parse bundle (serialized Program)
```

```python
from cobol_parse import parse_program
prog = parse_program(source_text)          # Program: paragraphs, data items, copybooks
```

The parse bundle is a **versioned, timestamp-free contract**: it records the sha256 of
the exact source text it parsed, refuses a newer version rather than partially reading
it, and parsing the same source twice produces identical bytes. `cobol-xstate
--from-parse prog.parse.json` models from it without re-parsing. See `parser/README.md`
for the pipeline stages and the faithfulness rule every consumer inherits.

## Develop

```bash
python -m pytest -q                        # both suites, nothing installed (root pyproject)
python tools/byteproof.py --check goldens/parse.sha256   # the byte-stability ratchet
```

The ratchet hashes the parse bundle a real `cobol-parse <example> --no-fetch` run writes
for every `examples/*.cbl`, estate-free. Re-record with `--record` **only** when a
bundle change is intended and reviewed. The downstream views (statechart, lineage,
reactive, …) are ratcheted where they are produced, by cobol-xstate-json's
`tools/gate.py` — a parse change that survives this repo's ratchet can still move bytes
there, so run both when touching the parser.

This repo was lifted out of cobol-xstate-json (its `core/` and `parser/` directories,
history preserved there — `git log --follow` in that repo reaches every pre-split
change).
