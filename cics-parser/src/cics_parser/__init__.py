"""cics_parser - the CICS definition front-end: physical text in, statements out.

Every reader of CICS definitions has to answer the same two questions before it can say
anything about a region, and answering them twice is how two readers of the same bytes
come to disagree:

    detect  : which physical format a source is - a DFHCSDUP deck, DFHCSDUP's printed
              LIST report, an assembler macro table deck, a BMS mapset, a BAS deck, a
              bundle manifest, a SIT - decided on content, never on suffix
    lexer   : the text of that format -> statements: ``lex_csd`` (command syntax, also
              BAS with its own verb set), ``lex_csd_report`` (the listing, emitted as the
              DEFINE statements a deck would have produced) and ``lex_macro`` (assembler
              columns 1-71, column-72 continuation resuming at 16)

What a statement MEANS - resource kinds, attribute edges, the install closure, the
same-name conflict rule - is the consumer's (``cics-dependencies`` first among them).
This distribution depends on nothing but the standard library.
"""

__version__ = "0.1.0"
