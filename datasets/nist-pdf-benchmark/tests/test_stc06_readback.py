"""Signed paired axes and the STC datum must survive ordinary PDF readback."""
from contextlib import nullcontext
from pathlib import Path
import sys
import build123d as bd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
import readback_stc06_nominal as reader

class Page:
    def __init__(self, text): self.text = text
    def get_text(self): return self.text

def pages(positive_axis=114.3, hole_count=20):
    contour = '\n'.join(['E01 LINE 0 0 10 0', 'E02 LINE 10 0 10 10',
                         'E03 LINE 10 10 0 10', 'E04 LINE 0 10 0 0'])
    result = [Page(f'PROFILE {name} | PLANE ' +
                   ('RZ' if name in ('BALL', 'CONE') else 'XZ' if name == 'RIB' else 'YZ') +
                   ' | dimensioned contour\n' + contour) for name in sorted(reader.NAMES)]
    result.append(Page('\n'.join([
        'BASE DATUMS: X=0 TO 234.95; Y=-152.4 TO 152.4; Z=0 TO 31.75.',
        'REAR WALL: X=0 TO 31.75.', 'FRONT BODY: X=209.55 TO 234.95.',
        'RIB: Y=-12.7 TO 12.7.',
        'LUG NOSES: Y=-108.585 -36.195 36.195 108.585; Z=50.8; R22.225; X=234.95 TO 247.65.',
        f'BALL AXES: X=107.95; Y=-114.3 AND {positive_axis}; FULL CIRCUMFERENCE 360 DEG.',
        'TAPERED AXES: X=158.75; Y=-76.2 AND 76.2; FULL CIRCUMFERENCE 360 DEG.',
        'SPHERICAL RECESS: CENTER 15.875 0 97.79; R12.7; OPEN Z=97.79; LOWEST Z=85.09.',
        'FRONT MARKS: ENTRANCE X=234.95; FLOOR X=232.41; DEPTH 2.54.',
        'STRAIGHT ROOT RADII: R3.175 ON ALL EIGHT INDICATED SPANS.',
        *[f'H{i:02d} AXIS X POINT 0 -127 78.74 D 14.2875 FROM 0 TO 17.4625' for i in range(1, hole_count + 1)],
        *[f'R{i:02d} FROM 0 0 0 TO 1 0 0' for i in range(1, 9)]
    ])))
    return result

@pytest.mark.parametrize('positive_axis,hole_count,error', [
    (114.4, 20, 'not symmetric'), (114.3, 19, 'Incomplete bore')])
def test_rejects_conflicting_axes_and_incomplete_bores(monkeypatch, positive_axis, hole_count, error):
    monkeypatch.setattr(reader.pymupdf, 'open', lambda _: nullcontext(pages(positive_axis, hole_count)))
    with pytest.raises(ValueError, match=error): reader.read_pdf('unused.pdf')

def test_signed_axial_intervals_transform_without_registration(monkeypatch):
    monkeypatch.setattr(reader.pymupdf, 'open', lambda _: nullcontext(pages()))
    spec = reader.read_pdf('unused.pdf')
    captured = {}
    def capture(old):
        captured.update(old)
        return bd.Solid.make_box(2, 3, 4, plane=bd.Plane(origin=(5, 7, -11)))
    monkeypatch.setattr(reader.common, 'reconstruct', capture)
    solid = reader.reconstruct(spec)
    assert captured['holes'][0]['center'] == [127, 78.74, 0]
    assert (captured['holes'][0]['bottom'], captured['holes'][0]['top']) == (-17.4625, 0)
    assert captured['base'] == (-152.4, 152.4, 0, 31.75, -234.95, 0)
    assert captured['ball'] == (114.3, -107.95, 360)
    assert list(solid.bounding_box().min) == pytest.approx([7, -7, 7])
    assert list(solid.bounding_box().max) == pytest.approx([11, -5, 10])
    assert solid.volume == pytest.approx(24)
