# PDF drawing → parametric part (v1)

### Optional 2D drawing context

`catia_inspect_drawing` accepts `drawingContextPath` pointing to a staged
`catia-drawing-context/1` JSON. The context contains the PDF SHA-256, drawing
asset ID, source hashes and selected 2D drawing records. A PDF hash mismatch is
rejected; the caller must establish the original PDF-to-drawing association.

An optional `blocks` source supplies raw block definitions and instances.
`blockQuery` can list frame summaries, page instances in one frame and
coordinate system, or page members of one definition. A returned matrix is the
source's unexpanded `transform_column_vectors`; nested transforms, external
references and layer visibility are not resolved. These records are separate
2D context, not PDF annotations or citable dimension evidence. The source hash
identifies the supplied block file; input provenance must also be checked by
the caller. This query has offline tests, not live end-to-end CATIA validation.

The evidence manifest keeps `contextEvidence` and `contextSource` separate
from page/OCR spans. IDs beginning `ctx:` identify drawing entities and retain
their source hash. Validators may cite the displayed text as explicit context
evidence, not as PDF/OCR; raw CAD measurements are not substituted for the
displayed annotation. Conflicting units still block a build. This context path
has offline tests; a complete Agent-driven CATIA run remains to be verified.

Status: implemented and verified on CATIA V5-6R2021 B31 SP7 (zh-CN) with the
fixture set in `F:\CatiaAgent\datasets\pdf-modeling`.

## Scope

Supported: a single part, flat rectangular plate, positive thickness, circular
**through** holes, text-layer PDF, dimensions that can be cited from the text
layer.

Not supported: assemblies, bent sheet / formed flanges, free-form surfaces,
blind / counterbored / countersunk / threaded holes, scanned or image-only PDFs.
Out-of-scope input returns `unsupported` or a readable read error; it is never
approximated.

## Why a fixed pipeline

The model reads the drawing, but three decisions are owned by code:

1. **unit normalization** — inch drawings are converted to millimetres once;
2. **provenance** — every dimension must cite a text span that contains the
   number;
3. **buildability** — the plan only contains whitelisted operations with
   server-computed sketch indices.

The agent cannot make a dimension trustworthy by writing a longer explanation,
and it cannot smuggle a script into the executor.

## Data flow

```
PDF
 │  catia_inspect_drawing
 ▼
catia-pdf-evidence/1            span ids (p<page>:s<index>, OCR p<page>:o<index>) + mm boxes
 │  agent writes a structured reading
 ▼
catia-build-requirements/1      numbers + evidence ids + clarifications
 │  catia_validate_requirements   (ready / needs_clarification / unsupported / invalid)
 ▼
normalized requirement           millimetres, hole centres, feasibility checks
 │  catia_compile_build_plan
 ▼
catia-build-plan/1              parameters + ordered whitelisted steps
 │  catia_build_part
 ▼
CATPart + independent report    reopened from disk, measured by a separate process
```

## `catia-pdf-evidence/1`

Written by `catia_inspect_drawing` to
`$CATIA_MCP_WORK_DIR/pdf-modeling/<sha256[:12]>/evidence.json` (when
`CATIA_MCP_WORK_DIR` is unset, the OS temp directory is used).

- `source`: absolute path, sha256, size, page count.
- `pages[].spans[]`: `id`, `text`, `source` (`text` or `ocr`), `bboxMm`
  (`[x0,y0,x1,y1]`, page top-left origin), font size; OCR spans also carry
  `confidence`.
- OCR: `includeOcr=true` renders each inspected page (default 400 dpi) and runs
  RapidOCR. Dimensions drawn as vector outlines (NIST FTC/CTC sheets) get
  `p<page>:o<index>` ids that can be cited exactly like text spans. `warnings[]`
  marks a page whose text layer is absent but OCR supplied spans.
- `pages[].dimensionCandidates[]`: numeric spans with `kindHint` (`diameter` /
  `radius`), `countHint` (`4x`), `throughHint` (`THRU`).
- `pages[].unitHints[]`: spans that name a unit.
- `warnings[]`: pages without a text layer.

Read errors are explicit: missing file, non-PDF, damaged/truncated PDF (MuPDF
had to repair it), page out of range, and PDFs with no usable text (even after
OCR) all raise a message a user can act on.

The input path does not need a ``.pdf`` suffix: AgentCPM's upload store keeps
files under an opaque client id, so the first 1 KiB is checked for ``%PDF-``
when the extension is not ``.pdf``.

## `catia-build-requirements/1`

```json
{
  "schema": "catia-build-requirements/1",
  "unit": "mm",
  "partName": "MOUNTING PLATE",
  "drawing": {"path": "...", "page": 1},
  "body": {"type": "flat_plate", "length": 120, "width": 80, "thickness": 10},
  "holes": {"count": 4, "diameter": 10, "through": true,
            "pattern": {"type": "symmetric", "edgeX": 20, "edgeY": 20}},
  "evidence": {"length": ["p1:s14"], "width": ["p1:s15"], "thickness": ["p1:s22"],
               "holeDiameter": ["p1:s20"], "holeCount": ["p1:s20"],
               "edgeX": ["p1:s18"], "edgeY": ["p1:s19"], "unit": ["p1:s5"]},
  "clarifications": [{"topic": "thickness", "question": "...", "answer": "10", "source": "user"}],
  "conflicts": [{"field": "length", "message": "...", "question": "..."}]
}
```

Patterns:

- `symmetric`: exactly 4 holes at `(±(L/2−edgeX), ±(W/2−edgeY))`.
- `explicit`: `pattern.centres = [[x,y], ...]` in model coordinates (plate centre
  at the sketch origin). Works for any even layout.

### Annular ring body

```json
{
  "body": {
    "type": "annular_ring",
    "outerDiameter": 63,
    "innerDiameter": 32,
    "thickness": 1.93,
    "edge": {"style": "rolled", "radius": 1.5}
  },
  "evidence": {"outerDiameter": ["p1:o11"], "innerDiameter": ["p1:o15"],
               "thickness": ["p2:o5"], "edgeRadius": ["p1:o29"], "unit": ["p1:o28"]}
}
```

`rolled` / `round` builds the web as a Pad and both rounded rims as one Shaft
whose profile holds two circles. The solid is taller than the web: the top of
each rim is at `2 × radius` above the bottom face. Validation requires
`outerDiameter > innerDiameter`, `radius < innerDiameter/2` and
`2 × radius > thickness`.

### Extruded profile body

```json
{
  "body": {
    "type": "extruded_profile",
    "thickness": 8,
    "profile": [
      {"type": "line", "x1": 0, "y1": 0, "x2": 80, "y2": 0},
      {"type": "line", "x1": 80, "y1": 0, "x2": 80, "y2": 30},
      {"type": "arc", "cx": 80, "cy": 30, "radius": 20, "startAngle": 0, "endAngle": 90}
    ]
  },
  "holes": {"count": 2, "diameter": 10, "through": true,
            "pattern": {"type": "explicit", "centres": [[20, 15], [60, 15]]}},
  "evidence": {"thickness": ["p1:s6"], "profile": ["p1:s2", "p1:s3"],
               "holeDiameter": ["p1:s7"], "holeCount": ["p1:s7"],
               "holePattern": ["p1:s8"], "unit": ["p1:s1"]}
}
```

- Coordinates are in the drawing unit and normalised once to millimetres.
- Segments are `line` (`x1,y1,x2,y2`) or `arc` (`cx,cy,radius,startAngle,endAngle` in
  degrees, CCW). Endpoints must chain within 0.01 mm and close on the first point.
- Holes are optional and must use `pattern.type = "explicit"`.
- Area is computed exactly from Green's theorem, so the expected volume does not
  depend on any measurement.
- Limitation: profile coordinates are fixed sketch geometry. Thickness and hole
  diameter/positions are named parameters; outline dimensions are not yet
  constraint-driven.

Validation rules:

| Code | Decision | Meaning |
| --- | --- | --- |
| `SCHEMA_MISMATCH` | invalid | wrong schema string |
| `UNIT_UNSUPPORTED` | unsupported | unit is not mm/inch |
| `BODY_UNSUPPORTED` | unsupported | not a flat plate or annular ring |
| `EDGE_STYLE_UNSUPPORTED` | unsupported | ring edge is not `rolled`/`round` |
| `BLIND_HOLES_UNSUPPORTED` | unsupported | `through` is false |
| `PATTERN_UNSUPPORTED` | unsupported | unknown pattern, or a symmetric pattern with ≠4 holes |
| `EVIDENCE_VALUE_MISMATCH` | invalid | the cited span does not contain the number |
| `EVIDENCE_REF_UNKNOWN` | invalid | span id is not in the manifest |
| `EVIDENCE_MISSING` | needs_clarification | a key dimension has no citation |
| `DIMENSION_CONFLICT` | needs_clarification | the reading declares an unresolved conflict |
| `HOLE_OUTSIDE_BODY` / `HOLES_OVERLAP` | invalid | geometry is impossible |
| `RING_DIAMETER_ORDER` | invalid | ring outer diameter ≤ inner diameter |
| `EDGE_RADIUS_TOO_LARGE` / `EDGE_RADIUS_TOO_SMALL` | invalid | rolled edge does not fit the hole / is buried in the web |
| `EDGE_BEADS_OVERLAP` | invalid | the two rims meet and leave no flat web |
| `PROFILE_INVALID` / `PROFILE_SEGMENT_INVALID` | invalid | malformed or too-short profile |
| `PROFILE_NOT_CLOSED` | invalid | consecutive segment endpoints differ by more than 0.01 mm |
| (missing unit/length/width/thickness/diameter/count) | needs_clarification | one question per topic |

Only `ready` can be compiled. Questions are returned once, in one list.

## `catia-build-plan/1`

- `parameters[]`: `L`, `W`, `T`, `D`, plus `EX`/`EY` (symmetric) or
  `LX<n>`/`LY<n>` (explicit). Every one is a CATIA length parameter.
- `steps[]`: whitelisted operations only — `new_part`, `create_parameters`,
  `create_sketch`, `rectangle`, `construction_rectangle`, `construction_line`,
  `line`, `arc`, `circle`, `constraint`, `close_sketch`, `pad`, `pocket`,
  `shaft`, `verify_geometry`.
- `expected`: analytic volume, bounding box, hole centres (plates) or cylinder
  radii (rings), feature count and parameter values. Nothing is measured from a
  built model.
- Shaft: the builder sets `RevoluteAxis` to the sketch's absolute vertical axis
  and assigns `FirstAngle.Value`; `AddNewShaft` alone leaves no axis and
  `Part.Update` fails with E_FAIL on B31 SP7.
- `planId`: sha256 of the canonical plan, excluding `planId`.

Topology (verified on B31): CATIA's sketch geometry collection puts the absolute
axis at index 1. Body sketch lines are 2..5 (bottom/right/top/left); the hole
sketch has four **construction** reference lines at 2..5 and circles from 6.

Constraints are bound to parameters with formulas created through the
constraint's dimension. On B31, `Relations.CreateFormula` accepts the **bare**
parameter name (`L`, `D / 2`, `EX - D / 2`); a backticked full path creates an
empty formula body and does not drive the dimension.

## Build and delivery

`catia_build_part` holds a machine-wide lock
(`%LOCALAPPDATA%\catia-mcp\catia-automation.lock`, `msvcrt.locking`) from the
first `new_part` until the editability check. Two MCP servers driving CATIA at
the same time activate each other's documents and break formula/constraint
creation; a second build waits (default 900 s, `CATIA_MCP_LOCK_TIMEOUT`) and
returns `AUTOMATION_BUSY` if it cannot acquire the lock.

`catia_build_part`:

1. validates requirements (never builds a non-`ready` reading);
2. compiles the plan;
3. refuses an existing `deliveryPath`;
4. builds in a new Part, checks the pad height, the volume after the pocket and
   the cylindrical faces against the analytic expectation;
5. saves to the new path and closes the document;
6. runs `scripts/independent_verify_part.py` in a **separate process**;
7. copies the delivery, changes `L` by +10 mm, updates, and checks that the
   parameter read-back and the volume changed; the copy is closed without saving
   and deleted;
8. reports `completed` only when the verifier returns `PASS` and the edit check
   passes.

## Independent verifier

`scripts/independent_verify_part.py` imports no `catia_mcp` code. It is spawned
with `stdin=DEVNULL` (an inherited MCP stdio pipe made the child block before
its first statement on this machine) and a timeout that kills the whole process
tree. It opens the CATPart from disk and checks:

`file_exists`, `com_available`, `catia_available`, `reopen`, `feature_count`,
`volume`, `bbox` (STL tessellation), `hole_count`, `hole_diameter`,
`hole_centres` (one-to-one matching), `hole_through` (cylindrical face height =
thickness), `parameters`, `parametric_formulas` (at least one formula
references a user parameter).

Volume and bounding box come from `SPAWorkbench` / an STL export; holes come
from cylindrical faces (`Measurable.Radius` + `COG` + lateral area), not from
the feature tree.

## Limits

- Flange/bend features, slots, counterbores and blind holes are out of scope.
- A symmetric pattern is limited to 4 holes; other layouts must use `explicit`.
- Evidence checking covers unit, length, width, thickness, diameter and count;
  for `explicit` patterns only the *existence* of the cited locating spans is
  checked (the arithmetic from pitch/edge to centre is the agent's reading).
- `pocket` depth is `T + 2mm` through a formula, not CATIA's "up to last" limit
  mode: the available `LimitMode` values did not produce a stable through-all on
  B31 (see the pitfall report).
- The independent verifier assumes a plate-like Z-through geometry; the
  cylindrical-face height test is what makes "through" measurable.
# 贯穿切除预检

组合特征的 `pocket` 可声明 `through: true`，表示沿草图法向该轮廓穿过当时存在的全部材料。
构建前的独立几何预检检查切除后轮廓投影内是否有剩余材料，发现残留时报
`THROUGH_CUT_INCOMPLETE`，阻止进入 CATIA。它不自动选择切除起点或方向；盲孔省略此字段。
是否为贯穿仍须根据图纸判断，该预检不保证图纸理解正确。

## 三维参考证据（受控生成模式）

运行环境由可信宿主绑定本次 `reference.step` 的哈希和大小。`catia_query_reference` 的
`faces` 查询支持分页及 `surfaceType: planar | cylindrical` 筛选，返回受支持面及未支持面数量；
每项含源哈希、STEP 坐标系、面积、质心和边界盒，圆柱另含半径、轴方向及轴上一点。
该轴位置是无限轴上的一点，并非圆柱几何中心；面数据目前仅供形状理解，不能作为参数证据。
如果宿主绑定了参考视图，`views` 分页列出视角，`view` 按 `viewId` 返回 MCP 内嵌 PNG。
每次读取重新检查当前 STEP、视图索引和图片哈希；视图仅为形状参考，不是图纸尺寸或识图成功证明。

新三维模式的可信宿主还传递 PDF 和二维上下文的哈希、大小；隔离服务只接受固定的 `drawing.pdf` 和 `drawing-context.json`，并在工具执行前后核对文件身份、大小和哈希。读图和裁图使用经身份及摘要验证的有界内存字节快照，二维图元、图层、块和派生圆测量读取同一上下文快照，OCR 使用独立 stdin 管道传送渲染像素，避免验证后按路径重开导致的瞬时替换。该模式的图纸证据登记在当前 MCP 实例中，大小上限为 32 MiB；后续校验和构建使用内存中登记的数据并检查磁盘回执，服务重启后须在新的证据目录重新检查图纸，不从原有磁盘文件恢复信任。执行期间检测到变化时，若涉及构建，其 CATIA 修改状态为未知，不能当作未修改或自动重试。上述保证不覆盖其他进程；旧模式仍按路径读取。
`edges` 查询会返回边长度、圆边半径、直径及圆心坐标的 `ref3d:` 证据，记录单位、来源哈希、
STEP 坐标框架和计算方式。组合特征需求中可将参数的 `source` 写为 `reference_inference`，
并在 `evidence` 中引用本次查询返回的 ID；参数值仍使用需求声明的单位，校验器执行单位换算并
检查真实数值。单位本身仍须引用图纸证据；如果同时引用图纸和参考，两者均须支持参数值。
参考值不是图纸标注，也不证明特征归属或坐标变换正确。

`summary`、`edges` 和 `faces` 将已核验的参考字节写入独占快照；Windows 下在 CAD 助手读取期间持有禁止写入和删除的文件句柄，并保护快照目录的祖先免于重命名。无法取得保护时查询失败，不登记参考证据；非 Windows 当前没有等效的非协作写保护，几何查询失败关闭，视图查询不受此限制。此保护不证明图纸与参考一致，也不能替代 CATIA 全局互斥。

证据注册表只存在当前 MCP 服务进程内，校验、编译和构建前重验参考文件；进程重启后必须重新
查询。仅支持已查询到的边字段，尚无面或视图证据，也没有自动识别所有未被引用的图纸与参考
冲突。不得将缺失的证据视为通过。

可信宿主可为独立只读预检设置 `CATIA_MCP_READONLY_PROBE=1`；该模式要求同时绑定生成目录、STEP、PDF 和二维上下文，只注册读图、裁图和参考查询，并不创建 CATIA 连接或编辑会话。其他值或缺少绑定时拒绝启动。该开关只收窄权限，不证明生产建模安全或替代全局互斥；预检结果不等于模型构建验收。
