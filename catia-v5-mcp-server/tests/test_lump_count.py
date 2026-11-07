"""Contract tests for the STL lump counter used by the edit sanity checks.

A parameter edit must not turn one solid into several. Pushing the hole margin
E past the plate edge leaves the circles outside the rectangle and CATIA pads
them as extra cylinders instead of failing the update; the bounding box grows
to cover them, so the holes-inside check still passes. The lump count is what
catches it (acceptance case A12).
"""

from __future__ import annotations

from catia_mcp.editing.probe import _lump_count

# Unit cube as (x, y, z) corners.
_CORNERS = [
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (1.0, 1.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (1.0, 0.0, 1.0),
    (1.0, 1.0, 1.0),
    (0.0, 1.0, 1.0),
]

_FACES = [
    (0, 1, 2, 3),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7),
]


def _cube_triangles(offset=(0.0, 0.0, 0.0), scale=1.0):
    points = []
    for a, b, c, d in _FACES:
        quad = [
            tuple((_CORNERS[i][axis] * scale) + offset[axis] for axis in range(3))
            for i in (a, b, c, d)
        ]
        points.extend([list(quad[0]), list(quad[1]), list(quad[2])])
        points.extend([list(quad[0]), list(quad[2]), list(quad[3])])
    return points


def test_single_closed_solid_is_one_lump():
    assert _lump_count(_cube_triangles()) == 1


def test_two_disconnected_solids_are_two_lumps():
    points = _cube_triangles() + _cube_triangles(offset=(5.0, 0.0, 0.0))
    assert _lump_count(points) == 2


def test_three_lumps_from_plate_and_two_cylinders():
    # What E=200 produced on FixtureA: the plate plus two stray cylinders.
    points = _cube_triangles() + _cube_triangles(offset=(-5.0, -5.0, 0.0), scale=0.2)
    points += _cube_triangles(offset=(5.0, -5.0, 0.0), scale=0.2)
    assert _lump_count(points) == 3


def test_float_noise_does_not_split_a_solid():
    # STL round-trips can perturb shared coordinates slightly; a 1e-5 mm
    # difference is tessellation noise, not a gap.
    noisy = _cube_triangles()
    noisy[3] = [value + 1e-5 for value in noisy[3]]
    assert _lump_count(noisy) == 1


def test_empty_tessellation_has_no_lumps():
    assert _lump_count([]) == 0
