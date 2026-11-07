"""Strict point classification guards against a false full-overlap boolean."""
import pytest
bd=pytest.importorskip('build123d')
from catia_mcp.cad_support import _difference_witnesses,_classify_point

def test_shifted_hole_has_material_only_points_on_both_sides():
    plate=bd.Solid.make_box(40,30,8)
    a=plate.cut(bd.Solid.make_cylinder(3,8,plane=bd.Plane(origin=(20,15,0))))
    b=plate.cut(bd.Solid.make_cylinder(3,8,plane=bd.Plane(origin=(20.2,15,0))))
    points=_difference_witnesses(a,b)
    assert all(p is not None for p in points)
    assert [_classify_point(s,points[0]) for s in (a,b)]==['inside','outside']
    assert [_classify_point(s,points[1]) for s in (a,b)]==['outside','inside']

def test_identical_solid_does_not_create_a_difference_witness():
    shape=bd.Solid.make_box(10,20,30)
    assert _difference_witnesses(shape,shape)==[None,None]
