"""Summarise the extracted reference feature trees into a T0 inventory.

Reads ``runs/nist-pdf-benchmark/sketches-20261028/<case>.json`` (produced by
``scripts/extract_catpart_sketches.py``) and writes
``datasets/nist-pdf-benchmark/feature-inventory.md``.

The inventory is verification-side material: it tells the data-preparation side
which features a derived drawing must dimension. It is not a modelling recipe
for the generation side.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
ROOT = BENCH.parents[1]
SRC = ROOT / "runs" / "nist-pdf-benchmark" / "sketches-20261028"
OUT = BENCH / "feature-inventory.md"


def feature_kind(name: str) -> str:
    return "".join(ch for ch in name if not ch.isdigit()).strip(".") or name


def main() -> None:
    rows = []
    for path in sorted(SRC.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        case = path.stem
        features = data.get("features") or []
        kinds = Counter(feature_kind(f["name"]) for f in features)
        sketches = data.get("sketches") or []
        element_total = sum(len(s["elements"]) for s in sketches)
        constraint_total = sum(len(s["constraints"]) for s in sketches)
        big = [
            f"{s['name']}({len(s['elements'])}元素/{len(s['constraints'])}约束)"
            for s in sketches
            if len(s["elements"]) >= 10
        ]
        rows.append({
            "case": case,
            "features": len(features),
            "kinds": ", ".join(f"{k}×{v}" for k, v in kinds.most_common()),
            "sketches": len(sketches),
            "elements": element_total,
            "constraints": constraint_total,
            "bigSketches": "；".join(big) or "—",
            "hasFeatureTree": len(features) > 1 or bool(sketches),
        })
    lines = [
        "# FTC/CTC 参考特征树清单（T0）",
        "",
        "来源：`runs/nist-pdf-benchmark/sketches-20261028/*.json`，由",
        "`catia-v5-mcp-server/scripts/extract_catpart_sketches.py` 从 V5-6R2019",
        "参考 CATPart 读出。**仅验证侧使用**：它说明派生图纸必须覆盖哪些特征，",
        "不是生成侧的建模答案。",
        "",
        "| 案例 | 特征数 | 特征类型 | 草图数 | 草图元素 | 约束 | 主要草图 |",
        "| --- | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['case']} | {row['features']} | {row['kinds']} | {row['sketches']} | "
            f"{row['elements']} | {row['constraints']} | {row['bigSketches']} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- 特征类型是 CATIA 中文界面名：凸台=Pad、凹槽=Pocket/Groove、孔=Hole、",
        "  矩形阵列/圆形阵列=Pattern、倒圆角=Fillet、倒角=Chamfer、旋转体=Shaft、",
        "  旋转槽=Groove、拔模=Draft、盒体=Shell、加强肋=Rib、用户阵列=UserPattern。",
        "- `FTC-07`、`FTC-10`、`FTC-11` 只有单个导入 `Solid`，没有草图；它们不能",
        "  提供建模配方，派生图纸只能依据 PMI + 几何测量。",
        "- 草图元素是数据准备侧的原始几何，不等于派生图纸的最终标注；派生图纸仍需",
        "  逐例做尺寸映射和独立审查。",
        "- 当前 1/17 完成（FTC-11）；其余 16 例的状态见",
        "  `runs/nist-pdf-benchmark/20261028/report.md`。",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} cases)")


if __name__ == "__main__":
    main()
