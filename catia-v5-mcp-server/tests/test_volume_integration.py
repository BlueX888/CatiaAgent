"""Curved, trimmed faces need consistent adaptive volume integration."""
import math
import pytest

bd=pytest.importorskip('build123d')
pytest.importorskip('cadgen')
from catia_mcp.cad_support import _boolean_volume, inspect_shape


def test_trimmed_underside_root_volume_is_integrated_consistently():
    r=math.sqrt(.5)
    def line(a,b,z):return bd.Edge.make_line((*a,z),(*b,z))
    def arc(a,m,b,z):return bd.Edge.make_three_point_arc((*a,z),(*m,z),(*b,z))
    z=-50
    outline=[line((0,-100),(400,-100),z),line((400,-100),(400,225),z),line((400,225),(300,225),z),
        arc((300,225),(300-50*r,175+50*r),(250,175),z),line((250,175),(250,150),z),
        arc((250,150),(225+25*r,150-25*r),(225,125),z),line((225,125),(0,125),z),line((0,125),(0,-100),z)]
    plate=bd.extrude(bd.Face(bd.Wire(outline)),amount=50,dir=(0,0,1))
    z=-100
    outline=[line((250,125),(400,125),z),line((400,125),(400,225),z),line((400,225),(300,225),z),
        arc((300,225),(300-50*r,175+50*r),(250,175),z),line((250,175),(250,125),z)]
    foot=bd.extrude(bd.Face(bd.Wire(outline)),amount=50,dir=(0,0,1))
    body=plate.fuse(foot).solids()[0].clean()
    roots=[]
    for e in body.edges():
        b=e.bounding_box();m=e.center()
        if abs(b.min.Z+50)<1e-5 and abs(b.max.Z+50)<1e-5 and str(e.geom_type).endswith('LINE') and (
            (abs(m.Y-125)<1e-5 and 250<m.X<400) or (abs(m.X-250)<1e-5 and 125<m.Y<150)):
            roots.append(e)
    assert len(roots)==2
    body=body.fillet(10,roots).solids()[0]
    # Independently checked convergence at relative epsilon 1e-8 and 1e-10;
    # the old non-adaptive integral is 5956709.132678, outside this interval.
    expected=5956709.132888
    assert inspect_shape(body)['volumeMm3']==pytest.approx(expected,abs=1e-5,rel=0)
    assert _boolean_volume('common',body,body,0)==pytest.approx(expected,abs=1e-5,rel=0)
