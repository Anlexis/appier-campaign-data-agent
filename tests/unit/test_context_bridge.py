# CMN-C2-289 - Unit tests: the outer/inner graph state bridge.
#
# The bridge is per-invocation state, so the property that matters is that a
# take is destructive: one request's campaign data must never be picked up by
# the next request that supplied none. The end-to-end proof that the value
# actually crosses the boundary lives in the boundary suite - a node-level
# test cannot tell "the bridge works" from "the subgraph forwarded it".

from src.graph.context_bridge import stash_caller_campaign, take_caller_campaign


def test_a_stashed_payload_is_returned_once():
    stash_caller_campaign('{"name":"Autumn Push"}')
    assert take_caller_campaign() == '{"name":"Autumn Push"}'


def test_the_take_is_destructive():
    stash_caller_campaign('{"name":"Autumn Push"}')
    take_caller_campaign()
    assert take_caller_campaign() == ""


def test_nothing_stashed_reads_as_empty():
    take_caller_campaign()
    assert take_caller_campaign() == ""


def test_an_empty_stash_clears_a_previous_one():
    stash_caller_campaign('{"name":"Autumn Push"}')
    stash_caller_campaign("")
    assert take_caller_campaign() == ""
