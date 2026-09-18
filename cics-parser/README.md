# cics-parser

The IBM CICS definition **front-end**: which physical format a source is, and its text
turned into statements. Pure Python standard library, no dependencies. This is the reader
underneath [`cics-dependencies`](https://github.com/paulhowarda-bit/cics-dependencies),
packaged on its own so a second reader of the same members parses the same bytes the
same way instead of carrying a grammar of its own.

```python
from cics_parser.detect import KIND_CSD, looks_like_csd_report, source_kind
from cics_parser.lexer import lex_csd, lex_csd_report, lex_macro

kind = source_kind(text)                      # "csd", "bms", "macro-table", ...
reader = lex_csd_report if looks_like_csd_report(text) else lex_csd
statements, flags = reader(text)
statements[0].verb, statements[0].first("GROUP"), statements[0].line
```

Three dialects, one module, because a member's dialect is not reliably declared:

- **CSD command syntax** (`lex_csd`) — a DFHCSDUP SYSIN deck or `EXTRACT` output: free-form
  to column 72, `*` comments, a statement running until the next command verb (never by
  indentation). It takes the verb set, which is how a CICSPlex SM BAS deck
  (`BAS_COMMANDS`) reuses it.
- **The DFHCSDUP `LIST` report** (`lex_csd_report`) — the printed listing a whole-region CSD
  dump usually is: no command verb anywhere, ASA carriage control in column 1 (decided per
  source), the report's print timestamp in the right margin, DFHCSDUP's messages around it.
  Emitted as the `DEFINE` statements a deck would have produced.
- **Assembler** (`lex_macro`) — macro table decks and BMS: columns 1-71, a column-72
  continuation resuming at column 16, the operand field ending at the first blank outside
  quotes and parentheses.

The column-72 margin is decided **per source**: content past column 80 cannot be sitting on
an 80-column card, so a file that has any is read at full width and says so once
(`margin_for`).

What a statement *means* — resource kinds, attribute edges, the install closure, the
same-name conflict rule — is not here; that is the consumer's.
