"""The estate-client contract, and the diagnostic that explains a client that breaks it."""

import pytest

from mainframe_artifacts.artifact_service import call_service
from mainframe_artifacts.protocol import (ArtifactFetcher, SynonymResolver,
                                          describe_fetcher, describe_synonym_resolver)


def full(name, type=None, copy=None):        # noqa: A002 - the wire keyword
    return "TEXT"


def narrow(name):
    return "TEXT"


def variadic(name, *args, **kwargs):
    return "TEXT"


@pytest.mark.parametrize("fn", [full, narrow, variadic])
def test_every_supported_client_shape_is_usable(fn):
    # The contract says both keywords are OPTIONAL: call_service drops them and retries,
    # so a one-argument client needs no adapter. That is the promise being pinned here.
    assert describe_fetcher(fn) is None
    assert call_service(fn, "ALPHA", "copybook", "/tmp").text == "TEXT"
    assert isinstance(fn, ArtifactFetcher)   # structural: it is callable


def test_a_client_taking_no_argument_is_explained_not_just_rejected():
    why = describe_fetcher(lambda: None)
    assert why is not None and "positional" in why


def test_a_client_demanding_extra_arguments_is_explained():
    def needs_more(name, library):
        return None
    why = describe_fetcher(needs_more)
    assert why is not None and "library" in why


def test_a_non_callable_is_explained():
    assert "not callable" in describe_fetcher(object())


def test_no_client_is_explained():
    assert describe_fetcher(None) == "no estate client was supplied"


def test_an_uninspectable_client_is_given_the_benefit_of_the_doubt():
    # print is a builtin whose signature cannot always be read. Unknown is not the same
    # as wrong, and this diagnostic must never manufacture a complaint it cannot support.
    assert describe_fetcher(print) is None


# --------------------------------------------------------------------------- #
# the synonym resolver contract
# --------------------------------------------------------------------------- #

def resolves(name):
    return None


def test_a_resolver_of_the_documented_shape_is_usable():
    assert describe_synonym_resolver(resolves) is None
    assert describe_synonym_resolver(lambda name, *rest: None) is None
    assert isinstance(resolves, SynonymResolver)


def test_a_resolver_taking_no_argument_is_explained():
    why = describe_synonym_resolver(lambda: None)
    assert why is not None and "resolver(name)" in why and "table name" in why


def test_a_resolver_demanding_extra_arguments_is_explained():
    def needs_more(name, schema):
        return None
    why = describe_synonym_resolver(needs_more)
    assert why is not None and "schema" in why and "resolver(name)" in why


def test_no_resolver_and_a_non_callable_are_explained():
    assert describe_synonym_resolver(None) == "no synonym resolver was supplied"
    assert "not callable" in describe_synonym_resolver(42)


def test_the_fetcher_diagnostics_did_not_change_wording():
    # Both describers share one inspection; the fetcher's words are pinned so sharing
    # it cannot have reworded a message a host may already be matching on.
    assert describe_fetcher(lambda: None) == (
        "takes no positional argument, so it cannot be called as fetcher(name) - the "
        "member name is always passed positionally")

    def needs_more(name, library):
        return None
    assert describe_fetcher(needs_more) == (
        "requires argument(s) library that this tool does not supply - a client is "
        "called as fetcher(name), optionally with type= and copy=")
