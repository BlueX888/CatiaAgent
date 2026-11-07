"""Incomplete hole data and conflicting floor depths must not become a solid."""
from contextlib import nullcontext
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
import readback_ftc08_nominal as reader

class Page:
    def __init__(self,text):self.text=text
    def get_text(self):return self.text

def pages(hole_count=20,mark_floor='143.47825000'):
    contour='\n'.join(['E01 LINE 0 0 10 0','E02 LINE 10 0 10 10','E03 LINE 10 10 0 10','E04 LINE 0 10 0 0'])
    result=[Page(f'PROFILE {name} | PLANE '+('YZ' if name.startswith('MARK') else 'XY')+' | dimensioned wall\n'+contour) for name in sorted(reader.NAMES)]
    dimensions='\n'.join([
        'HEIGHT DATUMS: BOTTOM Z=0; BASE TOP Z=10.16; WEB UNDERSIDE Z=44.831; TOP Z=48.26.',
        'BLIND GROOVE: FLOOR Z=46.99; DEPTH 1.27.',
        f'SIDE MARKS: ENTRANCE X=146.01825; FLOOR X={mark_floor}; DEPTH 2.54.',
        'EDGE RADII: LARGE R3.048; FRAME ROOT R1.524; GROOVE ROOT R0.762.',
        *[f'H{i:02d} X 0 Y 0 D 2 Z 44.831 TO 48.26 THRU' for i in range(1,hole_count+1)]])
    return result+[Page(dimensions)]

@pytest.mark.parametrize('count,floor,error',[(19,'143.47825','hole table'),(20,'144.74825','recess depth')])
def test_rejects_incomplete_holes_or_conflicting_depth(monkeypatch,count,floor,error):
    monkeypatch.setattr(reader.pymupdf,'open',lambda _:nullcontext(pages(count,floor)))
    with pytest.raises(ValueError,match=error):reader.read_pdf('unused.pdf')

def test_reads_all_holes_and_recess_dimensions(monkeypatch):
    monkeypatch.setattr(reader.pymupdf,'open',lambda _:nullcontext(pages()))
    result=reader.read_pdf('unused.pdf')
    assert len(result['holes'])==20
    assert result['marks']=={'face':146.01825,'floor':143.47825,'depth':2.54}
