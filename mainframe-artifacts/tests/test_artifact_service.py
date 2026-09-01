"""Loading a host-injected callable by ``MODULE:FUNC`` name."""

from mainframe_artifacts.artifact_service import (DEFAULT_FETCHER, load_callable,
                                                  load_fetcher)


def test_load_callable_finds_a_function_by_module_and_attribute():
    fn, why = load_callable("json:loads")
    assert why is None and fn("[1]") == [1]
    fn, why = load_callable("json.loads")          # the dotted form is accepted too
    assert why is None and fn("[2]") == [2]


def test_load_callable_rejects_a_spec_that_is_not_module_func():
    fn, why = load_callable("nocolon")
    assert fn is None and why == "'nocolon' is not MODULE:FUNC"
    fn, why = load_callable(":loads")
    assert fn is None and "is not MODULE:FUNC" in why


def test_load_callable_names_a_module_that_does_not_import():
    fn, why = load_callable("no_such_module_here_xyz:fn")
    assert fn is None
    assert why.startswith("could not import no_such_module_here_xyz (")
    assert "ModuleNotFoundError" in why


def test_load_callable_names_a_missing_attribute_and_a_non_callable():
    fn, why = load_callable("json:no_such_attribute")
    assert fn is None and why == "json has no attribute no_such_attribute"
    fn, why = load_callable("json:__name__")
    assert fn is None and why == "json:__name__ is not callable"


def test_load_fetcher_messages_are_unchanged():
    # load_fetcher shares the spec split with load_callable and nothing else: its
    # default-client wording is what every retrieval report has been quoting.
    fn, why = load_fetcher("nocolon")
    assert fn is None and why == f"'nocolon' is not MODULE:FUNC (e.g. {DEFAULT_FETCHER})"
    fn, why = load_fetcher("no_such_module_here_xyz:fn")
    assert fn is None and why.startswith("could not import no_such_module_here_xyz (")
    fn, why = load_fetcher("json:no_such_attribute")
    assert fn is None and why == "json has no attribute no_such_attribute"
    fn, why = load_fetcher("json:loads")
    assert why is None and fn is not None
