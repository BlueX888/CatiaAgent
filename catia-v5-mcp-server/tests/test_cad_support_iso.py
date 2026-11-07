import build123d as bd
import cadgen.eng_drawing as drawing
import pytest


from catia_mcp import cad_support as support


def test_small_isometric_label_stays_inside_frame():
    shape = bd.Box(190.5, 3.038, 279.4)
    sheet = drawing.Sheet(size='A3', scale=0.2)
    _, _, _, view = sheet.three_views(shape, iso=True)
    before = view.at
    label_width = drawing._text_width((view.label or view.name).upper(), 3.5)
    assert before[0] + label_width / 2 > sheet.width - drawing._MARGIN - 6
    support._fit_iso_label(sheet, shape, view)
    assert view.at[0] + label_width / 2 <= sheet.width - drawing._MARGIN - 6
    assert view.at[1] == before[1]
    assert view._arranged is None


def test_already_fitting_label_does_not_move():
    shape = bd.Box(50, 50, 50)
    sheet = drawing.Sheet(size='A3', scale=0.2)
    _, _, _, view = sheet.three_views(shape, iso=True)
    view.at = (sheet.width / 2, view.at[1])
    before = view.at
    support._fit_iso_label(sheet, shape, view)
    assert view.at == before


def test_label_that_cannot_fit_is_rejected():
    shape = bd.Box(50, 50, 50)
    sheet = drawing.Sheet(size='A3', scale=0.2)
    _, _, _, view = sheet.three_views(shape, iso=True)
    view.label = 'too long ' * 100
    with pytest.raises(ValueError, match='does not fit'):
        support._fit_iso_label(sheet, shape, view)
