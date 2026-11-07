"""Incomplete holes, inconsistent frames and overall dimensions must be rejected."""
from contextlib import nullcontext
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
import readback_ctc03_nominal as reader

class Page:
    def __init__(self,text):self.text=text
    def get_text(self):return self.text

def drawing(*,hole=True,reverse=False,bad_frame=False,overall=10):
    points=[(0,0),(10,0),(10,10),(0,10)]
    if reverse:points.reverse()
    profile='\n'.join(f'E{i:02d} LINE {a[0]} {a[1]} {b[0]} {b[1]}' for i,(a,b) in enumerate(zip(points,points[1:]+points[:1]),1))
    lines=['UNITS MM',f'OVERALL X {overall}  Y 10  Z 5.',
        'GLOBAL LIMITS: X=0 TO 10; Y=0 TO 10; Z=-5 TO 0.',
        'CONTENTS: FRAMES 1 PROFILES 1 WALLS 1 BENDS 0 HOLES 1 OPENINGS 0 RECESSES 0.',
        'FRAME F1 H 1 0 0 V 0 '+('-1' if bad_frame else '1')+' 0 N 0 0 1',
        'WALL PLATE FRAME F1 PROFILE OUTER D -5 TO 0']
    if hole:lines.append('H01 PANEL PLATE U 5 V 5 DIA 2 D -5 TO 0 THRU')
    return [Page('\n'.join(lines)),Page('PROFILE OUTER FRAME F1 | PLATE OUTER CONTOUR\n'+profile)]

@pytest.mark.parametrize('kwargs,error',[({'hole':False},'Incomplete dimension tables'),({'bad_frame':True},'orthonormal'),({'overall':11},'Overall dimension conflicts')])
def test_conflicting_or_missing_dimensions_are_rejected(monkeypatch,kwargs,error):
    monkeypatch.setattr(reader.pymupdf,'open',lambda _:nullcontext(drawing(**kwargs)))
    with pytest.raises(ValueError,match=error):reader.read_pdf('unused.pdf')

@pytest.mark.parametrize('reverse',[False,True])
def test_signed_surface_span_is_independent_of_contour_winding(monkeypatch,reverse):
    monkeypatch.setattr(reader.pymupdf,'open',lambda _:nullcontext(drawing(reverse=reverse)))
    solid=reader.reconstruct(reader.read_pdf('unused.pdf'))
    assert solid.volume==pytest.approx(500-5*__import__('math').pi)
    assert list(solid.bounding_box().min)==pytest.approx([0,0,-5])
    assert list(solid.bounding_box().max)==pytest.approx([10,10,0])
