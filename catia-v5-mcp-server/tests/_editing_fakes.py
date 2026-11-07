"""Offline CATIA doubles for the editing tests.

Nothing here imports win32com or starts CATIA. The doubles reproduce only the
small COM surface the editing layer reads (``FullName``, ``Parameters``,
``MainBody``, ``Part.Update``, ``Documents``); the model readers themselves are
replaced by :func:`read_model_from_part`, which builds a real ``read_model()``
shaped dict from a fake part. That keeps the coordinator's logic (validation,
baselines, rollback, idempotency) under test while the COM boundary stays
deterministic.
"""

from __future__ import annotations

from typing import Any, Iterable


class FakeUnit:
    def __init__(self, symbol: str = "mm") -> None:
        self.Symbol = symbol


class FakeParameter:
    """A CATIA parameter with a real readable/writable ``Value``."""

    def __init__(self, name: str, value: float, unit: str = "mm") -> None:
        self.Name = name
        self._value = float(value)
        self.Unit = FakeUnit(unit)
        self.value_writes = 0
        self.valuate_calls = 0

    @property
    def Value(self) -> float:
        return self._value

    @Value.setter
    def Value(self, value: float) -> None:
        self._value = float(value)
        self.value_writes += 1

    def ValuateFromString(self, text: str) -> None:
        self.valuate_calls += 1
        cleaned = str(text).strip()
        for suffix in ("mm", "deg"):
            if cleaned.lower().endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
        self._value = float(cleaned)


class FakeParameters:
    def __init__(self, parameters: Iterable[FakeParameter] = ()) -> None:
        self._items = list(parameters)
        self._by_name = {param.Name: param for param in self._items}

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, key: Any) -> FakeParameter:
        if isinstance(key, str):
            if key not in self._by_name:
                raise KeyError(key)
            return self._by_name[key]
        return self._items[int(key) - 1]


class FakeNamed:
    def __init__(self, name: str) -> None:
        self.Name = name


class FakeSketches:
    def __init__(self, names: Iterable[str] = ()) -> None:
        self._names = list(names)

    @property
    def Count(self) -> int:
        return len(self._names)

    def Item(self, index: int) -> FakeNamed:
        return FakeNamed(self._names[index - 1])


class FakeShapes:
    def __init__(self, names: Iterable[str] = ("Pad.1",)) -> None:
        self._names = list(names)

    @property
    def Count(self) -> int:
        return len(self._names)

    def Item(self, index: int) -> FakeNamed:
        return FakeNamed(self._names[index - 1])


class FakeBody:
    def __init__(self, name: str = "PartBody") -> None:
        self.Name = name
        self.Sketches = FakeSketches()
        self.Shapes = FakeShapes()


class FakePart:
    def __init__(self, parameters: Iterable[FakeParameter] = (), name: str = "Part2") -> None:
        self.Name = name
        self.Parameters = FakeParameters(parameters)
        self.MainBody = FakeBody()
        self.update_calls = 0

    def Update(self) -> None:
        self.update_calls += 1


class FakeDocument:
    def __init__(self, full_name: str, part: FakePart | None = None) -> None:
        self.FullName = full_name
        self.Part = part if part is not None else FakePart()
        self.closed = False

    def Close(self) -> None:
        self.closed = True


class FakeDocuments:
    def __init__(self, documents: Iterable[FakeDocument] = ()) -> None:
        self._documents = list(documents)

    def add(self, document: FakeDocument) -> None:
        self._documents.append(document)

    @property
    def Count(self) -> int:
        return len(self._documents)

    def Item(self, index: int) -> FakeDocument:
        return self._documents[index - 1]


class FakeApp:
    def __init__(self, documents: FakeDocuments | None = None) -> None:
        self.Documents = documents if documents is not None else FakeDocuments()
        self.DisplayFileAlerts = True


class FakeConnection:
    def __init__(self, app: FakeApp) -> None:
        self.app = app


def read_model_from_part(app: Any, doc: Any, part: Any) -> dict[str, Any]:
    """A faithful ``read_model()`` shape whose geometry follows the L driver.

    The length parameter ``Part2\\L`` scales volume and the X bounding-box
    dimension, so a successful apply produces a measurable delta while the
    hole stays well inside the plate. The part's other parameters are reported
    verbatim.
    """
    parameters = []
    length = 160.0
    for param in part.Parameters._items:
        if param.Name == "Part2\\L":
            length = float(param.Value)
        parameters.append(
            {
                "handle": param.Name,
                "name": param.Name,
                "value": float(param.Value),
                "unit": param.Unit.Symbol,
                "dimensionType": "LENGTH",
                "drivenBy": None,
                "kind": "driver",
                "writable": True,
            }
        )
    scale = length / 160.0
    half = length / 2.0
    return {
        "part": part.Name,
        "features": {
            "mainBody": part.MainBody.Name,
            "shapes": ["Pad.1"],
            "sketches": [],
            "bodies": [part.MainBody.Name],
        },
        "parameters": parameters,
        "relations": [],
        "geometry": {
            "volumeMm3": round(240000.0 * scale, 6),
            "areaMm2": 25000.0,
            "bbox": {
                "min": {"x": -half, "y": -30.0, "z": 0.0},
                "max": {"x": half, "y": 30.0, "z": 10.0},
                "dimensions": {"x": length, "y": 60.0, "z": 10.0},
            },
        },
        "holes": [
            {
                "center": {"x": 0.0, "y": 0.0, "z": 0.0},
                "radius": 6.0,
                "source": "Sketch.1",
                "sketch": "Sketch.1",
                "coordinateSpace": "sketch-local",
            }
        ],
        "limitations": [],
        "measurementPrecision": {
            "lengthToleranceMm": 0.01,
            "volumeToleranceMm3": 0.01,
            "volumeRelativeTolerance": 1e-6,
        },
    }
