"""The estate-client contract, and the diagnostic that explains a client that breaks it."""

import pytest

from cobol_xstate_core.artifact_service import call_service
from cobol_xstate_core.protocol import ArtifactFetcher, describe_fetcher


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
