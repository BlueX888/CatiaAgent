"""Locale-tolerant selection of topology elements (vertex/edge/face).

CATIA's search parser resolves type names in the *UI language*: on a zh-CN
installation ``Topology.Vertex`` / ``Type=Vertex`` both fail with
``CATIASelection.Search`` E_FAIL while ``Type=顶点`` resolves (踩坑报告 4,
re-confirmed on V5-6R2021 B31 SP7). The spellings are tried in order and a
rejected query raises: an unsupported query must never be reported as
"the shape has no vertices".
"""

from __future__ import annotations

from typing import Any, NamedTuple

# English first: it resolves on an English UI, and a language-mismatched
# term costs one failed call before the localized spelling is tried.
TOPOLOGY_SEARCH_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Vertex": ("Topology.Vertex,sel", "Type=Vertex,sel", "Type=顶点,sel"),
    "Edge": ("Topology.Edge,sel", "Type=Edge,sel", "Type=边线,sel"),
    "Face": ("Topology.Face,sel", "Type=Face,sel", "Type=面,sel"),
}


class TopologySearchError(RuntimeError):
    """No known search spelling is accepted by this CATIA installation."""


class TopologySearchResult(NamedTuple):
    count: int
    term: str


def search_topology(selection: Any, target: Any, kind: str) -> TopologySearchResult:
    """Select ``target``'s ``kind`` children and leave them selected.

    ``kind`` is one of ``Vertex`` / ``Edge`` / ``Face``. The returned count is
    the number of elements now in ``selection``; callers index them with
    ``selection.Item(n).Reference``.

    A query accepted by CATIA that matches nothing returns count 0. A query
    CATIA rejects (wrong UI language) moves on to the next candidate; when
    every candidate is rejected, :class:`TopologySearchError` is raised with
    each attempt so the failure is visible instead of looking like an empty
    shape.
    """
    try:
        candidates = TOPOLOGY_SEARCH_CANDIDATES[kind]
    except KeyError:
        raise ValueError(
            f"Unknown topology kind '{kind}'; expected one of "
            f"{', '.join(TOPOLOGY_SEARCH_CANDIDATES)}."
        ) from None

    attempts: list[str] = []
    for candidate in candidates:
        try:
            selection.Clear()
            selection.Add(target)
            selection.Search(candidate)
        except Exception as error:  # noqa: BLE001 - COM raises bare pywintypes errors
            attempts.append(f"{candidate}: {type(error).__name__}: {error}")
            continue
        return TopologySearchResult(int(selection.Count), candidate)

    raise TopologySearchError(
        f"No {kind.lower()} search query is accepted by this CATIA "
        "installation (tried: " + "; ".join(attempts) + "). The search "
        "type names follow the UI language; add this release's spelling "
        "to catia_mcp/topology.py."
    )
