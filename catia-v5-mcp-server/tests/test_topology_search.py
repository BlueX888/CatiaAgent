"""Contract tests for the locale-tolerant topology search (踩坑报告 4).

CATIA's Selection.Search resolves type names in the UI language, so the tool
layer must try the known spellings instead of hardcoding ``Topology.Vertex``
(a zh-CN install rejects it with E_FAIL). These tests pin three behaviours:

- an accepted spelling returns its count and leaves the elements selected;
- a rejected spelling is never reported as "no elements";
- when every spelling is rejected the error names each attempt.
"""

from __future__ import annotations

import pytest

from catia_mcp.topology import (
    TOPOLOGY_SEARCH_CANDIDATES,
    TopologySearchError,
    search_topology,
)


class FakeSelection:
    """Minimal stand-in for CATIA's Selection object."""

    def __init__(self, accepted: dict[str, int]) -> None:
        self.accepted = accepted
        self.items: list[object] = []
        self.added: list[object] = []
        self.queries: list[str] = []
        self._count = 0

    @property
    def Count(self) -> int:
        return self._count

    def Clear(self) -> None:
        self.items = []
        self._count = 0

    def Add(self, target: object) -> None:
        self.added.append(target)

    def Search(self, query: str) -> None:
        self.queries.append(query)
        if query not in self.accepted:
            raise RuntimeError(f"Search failed for '{query}'")
        self._count = self.accepted[query]
        self.items = [f"{query}#{i}" for i in range(1, self._count + 1)]


@pytest.mark.parametrize(
    ("kind", "accepted_term", "expected"),
    [
        ("Vertex", "Type=顶点,sel", 8),
        ("Edge", "Type=边线,sel", 12),
        ("Face", "Type=面,sel", 6),
    ],
)
def test_localized_term_is_used_when_english_is_rejected(kind, accepted_term, expected):
    selection = FakeSelection({accepted_term: expected})
    target = object()

    result = search_topology(selection, target, kind)

    assert result.count == expected
    assert result.term == accepted_term
    assert selection.queries[-1] == accepted_term
    # Every attempt re-scopes the selection to the target, so a failed query
    # cannot leak a stale selection into the next candidate.
    assert len(selection.added) == len(selection.queries)


def test_english_term_wins_on_an_english_install():
    selection = FakeSelection({"Topology.Vertex,sel": 8})

    result = search_topology(selection, object(), "Vertex")

    assert result == (8, "Topology.Vertex,sel")
    assert selection.queries == ["Topology.Vertex,sel"]


def test_accepted_query_with_no_matches_returns_zero():
    # 0 from an accepted query is a real answer (e.g. a fully curved solid has
    # no vertices) and must not be confused with a rejected query.
    selection = FakeSelection({"Topology.Vertex,sel": 0})

    result = search_topology(selection, object(), "Vertex")

    assert result.count == 0
    assert result.term == "Topology.Vertex,sel"


def test_rejected_everywhere_raises_with_all_attempts():
    selection = FakeSelection({})

    with pytest.raises(TopologySearchError) as error:
        search_topology(selection, object(), "Face")

    message = str(error.value)
    assert "face" in message
    for candidate in TOPOLOGY_SEARCH_CANDIDATES["Face"]:
        assert candidate in message, candidate
    assert selection.queries == list(TOPOLOGY_SEARCH_CANDIDATES["Face"])


def test_unknown_kind_is_a_programming_error():
    with pytest.raises(ValueError):
        search_topology(FakeSelection({}), object(), "Loop")
