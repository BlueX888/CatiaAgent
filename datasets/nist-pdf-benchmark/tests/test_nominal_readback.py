"""Drawing-coordinate direction is independent of contour winding."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('ctc_reader',Path(__file__).parents[1]/'tools/readback_ctc01_nominal.py')
reader=importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


@pytest.mark.parametrize('reverse',[False,True])
def test_inner_and_outer_contours_respect_the_same_dimensioned_z_band(reverse):
    points=[(0,0),(10,0),(10,10),(0,10)]
    if reverse:points.reverse()
    profile={'plane':'XY','edges':[{'type':'LINE','start':a,'end':b}
        for a,b in zip(points,points[1:]+points[:1])]}
    solid=reader.profile_prism(profile,-50,50)
    assert solid.volume==pytest.approx(5000)
    assert list(solid.bounding_box().min)==pytest.approx([0,0,-50])
    assert list(solid.bounding_box().max)==pytest.approx([10,10,0])
