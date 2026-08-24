"""Alternative producers of the parse-bundle contract.

The native recursive-descent parser is the default producer and needs nothing beyond
the standard library. Everything under this package is OPTIONAL: an external parser,
run as a separate process, judged against the same contract. Nothing here is imported
by the default parse path.
"""
