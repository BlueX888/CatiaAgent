"""Pin the CATIA API mode: an angle must not be interpreted as Length2."""
from types import SimpleNamespace

from catia_mcp.tools.part_design import PartDesignTools


def test_length_angle_chamfer_passes_angle_in_the_correct_mode(monkeypatch):
    calls = []
    updates = []
    feature = SimpleNamespace(Name="Chamfer.1")
    def create(*args):
        calls.append(args)
        return feature
    part = SimpleNamespace(ShapeFactory=SimpleNamespace(AddNewChamfer=create), UpdateObject=updates.append)
    connection = SimpleNamespace(ensure_connected=lambda: None, get_active_part=lambda: part,
        get_active_part_body=lambda: None, refresh_display=lambda: None)
    tools = PartDesignTools(connection)
    reference = object()
    monkeypatch.setattr(tools, "_topo_reference", lambda kind, index: reference if (kind, index) == ("Edge", 9) else None)
    tools.execute("catia_chamfer", {"length": 2, "angle": 45, "edge_name": "Edge.9"})
    # PartTypeLib.tlb: catMinimalChamfer=1, catLengthAngleChamfer=1,
    # catNoReverseChamfer=0. Mode 0 interprets 45 as a 45 mm second length.
    assert calls == [(reference, 1, 1, 0, 2, 45)]
    assert updates == [feature]
