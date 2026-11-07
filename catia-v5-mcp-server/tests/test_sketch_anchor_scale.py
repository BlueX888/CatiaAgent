from catia_mcp.drawing.feature_executor import _sketch_anchor


def test_reference_axes_scale_to_profile_and_revolution_axis():
    feature = {"sketch": {"profiles": [{"points": [{"x": {"value": -20, "formula": "-20mm"},
                                                         "y": {"value": 30, "formula": "L"}}]}]},
               "axisX": {"value": -200, "formula": "X"}}
    assert _sketch_anchor(feature) == 1601
    feature["axisX"]["value"] = 0
    assert _sketch_anchor(feature) == 1000
