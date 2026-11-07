---
name: CATIA 参数化改型
description: 修改已保存的单实体参数化 CATPart 尺寸时使用，按 catia-edit/1 走 begin → 任务上下文 → 意图编译 → 计划 → apply → finalize；带证据区分孔/凸台、模型坐标与草图坐标系，并在澄清后按状态闸门继续。用户上传 PDF 图纸要从零新建零件时不要用本技能，改用 catia-pdf-model。
---

# CATIA 参数化改型（catia-edit/1）

## 1. 适用范围

> **路由**：输入是 PDF 工程图、要求从零创建零件 → 用 `catia-pdf-model` 技能（`catia_inspect_drawing` → `catia_build_part`）。本技能只改**已存在**的 `.CATPart` 参数。

- 支持：已保存的**单实体参数化 `.CATPart`**；由驱动参数控制的长度、宽度、厚度、孔径/半径、孔边距等尺寸改型。
- 不支持（直接拒绝并说明，不要用通用工具拼凑）：新增特征、装配/Product、自由曲面/GSD、无参数导入实体、多实体联动、移动单个孔心、只改某一侧的孔位置。
- 本技能只用 11 个工具（6 个编辑工具 + 4 个只读辅助 + 1 个回退写入），wire name 为：
  - `mcp__catia__catia_begin_edit`
  - `mcp__catia__catia_inspect_edit`
  - `mcp__catia__catia_get_task_context`（只读：按任务筛选候选、角色、坐标系、关系和保持能力）
  - `mcp__catia__catia_compile_intent`（只读：把结构化意图编译成计划，不改模型）
  - `mcp__catia__catia_find_cases`（只读：按结构和操作兼容性检索已验证案例模板）
  - `mcp__catia__catia_apply_edit_plan`
  - `mcp__catia__catia_get_edit_status`
  - `mcp__catia__catia_get_edit_history`（只读：多轮状态、原始基准、最近操作）
  - `mcp__catia__catia_revert_last_edit`（写：恢复上一笔已验证操作的参数）
  - `mcp__catia__catia_finalize_edit`
  - `mcp__catia__catia_recover_edit`
- 如果某个辅助工具在当前环境不存在（例如对照臂只装了子集），退回 `catia_inspect_edit` 读完整快照；不要伪造工具结果。

## 2. 唯一工作流

1. **澄清歧义**（见 §7）。有疑问时这一步不产出任何写入。
2. **`catia_begin_edit`**，入参 `sourcePath`（绝对路径）。读返回的 `data.editSessionId`、`data.modelRevision`、`data.snapshot`。
   - `ok:true` + `code:"ALREADY_BOUND"`：同一源文件已有 `ready`/`verified` 会话，直接用返回的 `editSessionId`，不要重复 begin。
   - begin 会对服务端工作副本上的每个驱动参数做扰动探针（最后恢复原值），因此偏慢是正常的；源文件不动。
   - 工作副本默认落在**源文件同目录**的 `.catia-edit/<editSessionId>/`（即留在当前项目内）；只有在用户明确要求时才传 `workingDir` 覆盖。
   - `data.workingCopyPath` 是工作副本的真实路径，汇报时直接引用它，不要自己拼。
3. **读任务上下文**：
   - 已知要改的量（长度/宽度/孔径/半径等）时，先调用 `catia_get_task_context`，入参 `editSessionId`、`attribute`，并把用户原话放进 `text`。它只返回本任务相关的候选 handle、当前值、单位、实测圆角色、草图坐标系、相关关系和保持能力，并保留完整快照入口，比读整个 1.6 MB 快照更快。
   - 需要看完整结构、全部参数或任务上下文缺项时，再用 `catia_inspect_edit` 读 `snapshot`，按 §3 选参数。
   - 多轮改型先调用 `catia_get_edit_history`：`baselineParameters` 是 begin_edit 时的“原始值”，`currentParameters` 是当前值；"再加 10"用当前基准，"比原始加 10"用 `reference.basis:"original"`。
   - `catia_find_cases` 只返回**模板**（属性/操作/scope/preserve/acceptance 的组合），不含 handle、旧 revision 或旧目标值；命中后仍必须用当前快照重新编译，不得复用旧 handle。
4. **写 `cad-intent-draft/1` 并调用 `catia_compile_intent`**，按 §4.0。**不要自己心算相对量、单位换算或直径/半径**：这些由编译器确定性完成。
   - 工具入参的 `userText` 由平台从本轮用户原话注入，**不要填写或改写**；编译器用它核对 `intent.text`。
   - 上一轮返回过 `needs_clarification` 时，只有用户真的回答了才能继续：本轮 compile 必须带 `userConfirmed:true` 和 `confirmationText`（逐字取自用户回答），否则会被闸门挡回。详见 §4.1。
   - `data.decision:"planned"` → 直接使用返回的 `data.plan`（不要改 handle、newValue、preserve、acceptance）。
   - `data.decision:"needs_clarification"` → 按 `data.unresolved` 和 `data.candidates` 向用户提问，**零写入**。
   - `data.decision:"unsupported"` → 原样说明原因，**零写入**；不得手写计划绕过。
   - `data.decision:"invalid"` → 按 `data.problems` 修正意图草稿或报告问题；不得手写计划绕过。
5. **`catia_apply_edit_plan`**，入参 `editSessionId` 和 `plan`（优先传第 4 步返回的 `data.plan`；整个对象，不要传 JSON 字符串）。逐项检查 `ok`、`code`、`data.verdict`、`data.businessState`、`data.modelRevision`、`data.checks`、`data.recovery`。
6. **只有 `data.verdict:"PASS"` 且 `data.businessState:"verified"` 才能继续**；否则按 §6 处理，不要靠重试掩盖失败。
7. **`catia_finalize_edit`**（仅当 `businessState:"verified"`），入参 `editSessionId`、`deliveryPath`：必须是**新的、绝对的、尚不存在**的 `.CATPart` 路径（已存在 → `SAVE_CONFLICT`，不覆盖）。成功返回 `ok:true`、`data.businessState:"completed"`、`data.delivery{path,sha256,sizeBytes,reportJson,reportMarkdown}`、`data.verification.checks`。

重试规则：**只有服务端明确要求时才重试**。返回信封里的 `recovery.automatic_retry_allowed` 恒为 `false`，不构成重试许可；`data.idempotentReplay:true` 只说明这是重放，不是新的写入。需要重做时必须换新 `operationId`、重新读取 `modelRevision` 后重建计划；不要重复提交同一计划碰运气，也不要在 `failed`/`unknown` 上强行推进。

`catia_get_edit_status` / `catia_inspect_edit` 只读，用来刷新 revision 和取证，不能替代上面的步骤。

## 3. 快照怎么读

- `snapshot.parameters[]` 每项至少包含 `handle`、`name`、`value`、`unit`、`kind`、`writable`；驱动参数还会带 `dependencies`（`affectedParameters`、`geometryChanged`、`holeCentersMoved`、`holeWorldCentersMoved`、`holeRadiiChanged`、`holeRadiusDeltas`、`bboxEdgeDeltas`）。
- `snapshot.structure` 是 P1 的结构证据，必须按证据强度读：
  - `structure.features[]`：`name`、`kind`、`materialRole`、`kindEvidence`、`sketch`。本机 `Shape.Type` 不存在（踩坑 5），`kind`/`materialRole` 来自**特征名标记**，`kindEvidence.strength` 为 `"name_only"`，只能当候选，不能当实测事实。
  - `structure.sketches[]`：`frame`（`origin`/`horizontal`/`vertical`/`plane`/`normalAxis`，来自 `Sketch.GetAbsoluteAxisData`）、`usedByFeatures`、`circles[]`、`constraints[]`（`constraint.Dimension.Name` 直接指向参数 handle）。
- `snapshot.holes[]` 的 `role` 是**实测**语义：`hole`（半径增大时体积减小）、`additive_feature`（半径增大时体积增大）、`observed_circle`（没有可写 driver 改变它）、`insufficient_evidence`（半径变化与边界变化耦合）。`roleEvidence` 记录 driver、半径增量、体积增量。另有 `center`（草图局部）与 `centerWorld`（模型坐标，来自草图坐标系）。
  - **只有 `role:"hole"` 才能当孔**。`additive_feature` 是凸台/圆柱，不能按孔径修改；`observed_circle`/`insufficient_evidence` 不能宣称是孔。
  - 孔数量断言用实测孔数，不是圆的总数；`holes[]` 里可能同时有孔、凸台圆和未定圆。
- `snapshot.preserves` 给出每种保持能力的 `supported`/`reason`；`hole_centers.worldSupported` 为 true 时可用 `hole_world_position`（模型坐标）。
- `snapshot.relationGraph` 是带证据的邻接表：`explicit_formula`（公式文本真的引用了该参数）、`explicit_geometric_constraint`（草图约束的 Dimension 指向该参数）、`explicit_feature_reference`（特征自身返回的草图）、`perturbation_impact`（扰动实测影响）、`unconfirmed`（仅名称相似）。`unconfirmed` 不能用来宣称保持条件已被证明。
- **只有** `kind` 为 `"driver"` **且** `writable` 为 `true` 的参数允许出现在 `changes[]`。`writable` 为 `false`/`null`、或 `kind` 不是 `driver` 的，一律不可写。
- `handle` 是参数全名（**本地化**，例如 `Part1\L`）。写进 `changes[].parameter` 时逐字复制，包括反斜杠与中文；不得翻译、规范化、拼造、按 `name` 猜或用显示名替代。在 JSON 里反斜杠要写成 `\\`，但字符串值必须与 `handle` 完全相同。
- `value` 是当前值，`unit` 是参数单位（`mm`/`deg`，也可能是 `none`/`unknown`）。
- `capabilities.invariants.holes_edge_distance.{supported,reason}` 决定能否使用 `preserve`；`capabilities.writableDrivers`、`capabilities.geometryDrivers` 可交叉核对。
- `relations[]` 是公式原文，用来理解哪些参数被公式驱动。
- `geometry.{volumeMm3,bbox}`、`holes[]`（`center`、`radius`）是写 `acceptance` 的唯一数据来源。`geometry.bbox.exact` 为 `false`、bbox 只有 `error`、或数值缺失时，不能当作精确证据，也不要编造期望值。
- `limitations[]` 非空必须读：它说明哪些测量不可用，直接影响能写什么断言。

## 4.0 先编译结构化意图（`cad-intent-draft/1`）

**不要自己心算相对量、单位换算或直径/半径，也不要凭参数名猜方向。** 先把用户话整理成下面的草稿，交给 `catia_compile_intent`；它用快照里的实测证据做绑定和算术，并返回可直接执行的计划。

```json
{
  "editSessionId": "<begin_edit 返回的 id>",
  "intent": {
    "schema": "cad-intent-draft/1",
    "text": "左右各延长 50 mm，中心不动",
    "decision": "planned",
    "target": {
      "parameter": "Part1\\L",
      "attribute": "length"
    },
    "operation": {
      "type": "increase",
      "amount": 50,
      "unit": "mm",
      "scope": "each_side"
    },
    "reference": {
      "basis": "current",
      "revision": 0,
      "frame": "model",
      "axis": "x"
    },
    "preserve": ["center_position"]
  }
}
```

字段语义：

- `text`：**必须是用户本轮的原话**，逐字复制。不得加入单位、括号说明、假设或"我理解为"之类的改写；编译器会用它与结构化字段做一致性检查，改写会让检查失效。
- `target.parameter`：快照里的**精确 handle**。如果用户没有点名参数，而多个参数都可能满足同一属性，就留 `null`，让编译器返回候选；不要随机挑一个。
- `target.attribute`：用户说的**量**，不是参数名。取 `length` / `width` / `height` / `thickness` / `diameter` / `radius` / `angle`。编译器会根据实测判断参数本身是直径还是半径、驱动哪根轴；用户说直径而参数是半径（或反之）时由它换算。
- `target.binding`：只有当**用户点名了参数**（例如"把 P1 改成 100"）才写 `"user_explicit"`；否则省略。省略后，若多个参数驱动同一尺寸，编译器会要求澄清，而不是替你选。
- `target.holeCount`：用户提到孔的数量时写正整数（如"三个孔"写 3）。编译器会与**该参数实际驱动的实测孔数**比对；不一致时要求澄清，而不是改掉现有的孔。
- `target.kind`：圆形特征的类型，`hole` 或 `additive_feature`。用户说"孔/孔径"写 `hole`；说"凸台/圆柱"写 `additive_feature`。编译器按 `holes[].role` 过滤候选：孔驱动不会被拿去改凸台，反之亦然；两侧都驱动时要求澄清。
- `target.sketch`：圆形特征所在草图名（如 `草图.3`）。同一属性有多个孔组时用它限定，例如"侧面的孔"。编译器会核对参数确实驱动该草图的圆。

  - `target.kind`、`target.sketch`、`target.holeCount` 都是**结构化判别字段**，只有用户原话支持时才能填：说"孔/凸台"才填 kind，说"侧面/顶面/安装/草图名"才填 sketch，说"两个/三个孔"才填 holeCount。编译器会用 `target.*.provenance` 拒绝模型自造的取舍；被拒绝时不要换一个 sketch 再猜，按 `unresolved` 请用户明确孔组。
- `operation.type`：`set`（绝对，"改为 200"）/ `increase`（"增加/加长"）/ `decrease`（"减少/缩短"）。
- `operation.amount`：正数。`set` 时是用户单位下的绝对目标；相对操作时是增量。
- `operation.unit`：`mm` / `cm` / `in` / `deg` / `none`。**用户没说单位时省略该字段，让编译器返回 `needs_clarification`；不得猜 mm 或 cm。**
- `operation.scope`：`overall`（总尺寸变化，如"总长加 50"）/ `each_side`（两侧各自变化，如"左右各加 50"，总长 +100）/ `single_side`（只动一侧，必须给 `reference.direction`）。`set` 只能用 `overall`。
- `reference.basis`：`current`（当前快照，默认）/ `original`（本会话 begin 时的值，用于"比原始长度"）。原始值不存在时编译器会要求澄清，不要从对话里估算。
- `reference.revision`：当前 `modelRevision`；与快照不一致时编译器会要求重读。
- `reference.frame`：`model`（默认）或 `sketch:<草图名>`。`view` 不是稳定参照，编译器会拒绝。
  - 用 `sketch:<名>` 时，必须同时给 `reference.directionText`（`left`/`right`/`up`/`down`，也接受 左/右/上/下），编译器用该草图的 `GetAbsoluteAxisData` 把方向映射到模型轴；映射与实测驱动轴不一致时澄清。非 XY 草图不能凭视图方向猜左右。
- `reference.axis`：可选 `x` / `y` / `z`；与实测轴不一致时编译器会拒绝或要求确认。
- `reference.direction`：`single_side` 必填，取 `positive` / `negative`。左/右必须先和用户确认对应哪根模型轴，不得按当前视图猜。
- `preserve`：支持 `center_position`、`hole_edge_distance`、`hole_centers`、`hole_world_position`、`other_dimensions`。
  - `hole_centers` 保持草图局部孔心；`hole_world_position` 保持模型坐标孔心（需要 `preserves.hole_centers.worldSupported=true`）。用户说"孔心不动"且模型有非 XY 草图时，优先同时确认模型坐标。
  - **编译器说不可执行时，不得删掉这一项手工提交**；把原因告诉用户。
- `decision`：一般为 `planned`；如果你已经判断请求有歧义或超出范围，可写 `needs_clarification` / `unsupported`，并在 `unresolved` 写明原因。
- 工具入参的 `operationId` 可以省略：编译器会按会话、revision、目标值和 preserve 生成一个；不要复用上一轮的 `operationId`，否则 `apply` 可能返回 `OPERATION_CONFLICT`。

编译器返回：

- `data.decision:"planned"`：`data.plan` 已是合法的 `catia-edit-plan/1`，直接提交，不要改动其中的 handle、`newValue`、`preserve`、`acceptance`。
- `data.decision:"needs_clarification"`：按 `data.unresolved` 提问；`data.candidates` 给出候选参数的 handle、当前值、单位、轴/孔径语义，提问时带上它们。零写入。
- `data.decision:"unsupported"`：原样说明原因（例如单侧不可表达、边距不可保持、参数不驱动几何），零写入；不得手写计划绕过。
- `data.decision:"invalid"`：按 `data.problems` 修正草稿或报告；不得手写计划绕过。
- `data.binding` / `data.checks` 是取证：包含 handle、当前值、单位、`meaning`、`axis`、`ratio` 和实测证据。汇报时可以引用，但不要据此改写数值。
- 编译器是只读的；它不替代 `catia_apply_edit_plan` 的服务端校验，计划仍要按 §5/§6 正常执行和判定。

**`needs_clarification` 是停止信号，不是重试信号。** 不得通过改写 `text`（例如自行补上单位或加括号说明）、删掉 `preserve`、换一个参数或改 `scope` 后重新编译来绕过它；只能按 `unresolved` / `candidates` 向用户提问。`intent.text` 必须是平台注入的 `userText` 的逐字片段；改写会被 `text.provenance` 拒绝。

## 4.1 状态化澄清闸门（必须遵守）

编译器把两种 `needs_clarification` 分开：
- `data.clarificationGate.armed:false` + `data.openAmbiguity`：**模型可自行修正**（缺 handle、同名候选需要 sketch/kind 取舍等）。可以用候选里的精确 handle/sketch/kind 重试；重试不会污染用户澄清状态。但 apply 仍会被 `AMBIGUITY_OPEN` 挡住，直到某次 compile 真的返回 `planned`。
- `data.clarificationGate.armed:true` + `data.pendingClarification`：**需要用户回答**（缺单位、方向、原始基准、孔数量不符、孔/凸台语义冲突、preserve 冲突等），保存**平台注入的用户原话**。

- 在用户回答之前：
  - 再次调用 `catia_compile_intent` 想得到 `planned` 会被闸门转回 `needs_clarification`，并带 `data.pendingClarification`。
  - 直接调用 `catia_apply_edit_plan` 会返回 `CLARIFICATION_PENDING`，`effects.modified:false`；手写计划也不能绕过。
- 用户回答后（同一会话、同一 editSessionId）：
  - 新草稿按用户回答重新绑定；
  - 调用 `catia_compile_intent` 时带 `userConfirmed: true`；
  - `confirmationText` **逐字复制用户回答里能消解歧义的那一句**（例如 `单位是 mm`），它必须是本轮 `userText` 的子串；
  - 编译器核对通过后才会清闸门并返回 `planned`；返回里会有 `data.clarificationCleared` 取证。
- 如果用户改口成另一件事（不是回答原问题），也要带 `userConfirmed:true` 和用户新话里的原句；不要用旧的澄清条件继续。
- 没有平台注入的 `userText`（离线调用）时，闸门退化为按 `intent.text` 判断；但真实回合必须依赖平台注入，不能自己声称“用户确认了”。

## 4. 计划格式：`catia-edit-plan/1`

必填字段：`schema`、`editSessionId`、`operationId`、`expectedRevision`、`changes`（不可为空）；`intent`、`preserve`、`acceptance` 可选。

```json
{
  "schema": "catia-edit-plan/1",
  "editSessionId": "<begin_edit 返回的 editSessionId>",
  "operationId": "len-260-0001",
  "expectedRevision": 0,
  "intent": { "text": "长度左右各延长 50", "resolved": "绝对长度 = 160 + 2×50 = 260 mm" },
  "changes": [
    { "parameter": "Part1\\L", "oldValue": 160.0, "newValue": 260.0, "unit": "mm", "reason": "左右各延长 50 mm，共 +100 mm" }
  ],
  "preserve": [],
  "acceptance": [
    { "type": "bbox_dimension", "axis": "x", "expected": 260.0 }
  ]
}
```

- `expectedRevision`：必须等于最近一次快照或 `catia_get_edit_status` 的 `modelRevision`（begin 后为 0）。正常执行路径（`PASS` 或失败判定）会把它 +1；`ALREADY_SATISFIED` 不写也不加；执行异常回滚（`INCONCLUSIVE` + `rolled_back`/`unknown`）不加，且此时会话已不能继续写入。所以**重试前必须重新读取**。
- `operationId`：每个新计划用新值。同一 `operationId` + 完全相同的计划 = 幂等重放（返回原结果，`data.idempotentReplay:true`）；同一 `operationId` + 不同计划 = `OPERATION_CONFLICT`。
- `changes[]` = `{parameter, oldValue, newValue, unit, reason}`：
  - `parameter`：快照里的 `handle`，逐字。
  - `oldValue`：从快照 `value` 抄，不估算、不改写。
  - `newValue`：**绝对**目标值；`mm` 参数必须 > 0，绝对值 ≤ 1e6。
  - `unit`：必须等于该参数的 `unit`（`mm`/`deg`），**不做单位换算**。
- `intent`：`{text: 用户原话意图, resolved: 你推导出的绝对目标}`；相对量必须在这里写清算式。

## 5. 硬规则

### 5.1 相对量先换算成绝对值，再写计划

**优先由 `catia_compile_intent` 完成。** 下面的规则用于核对编译器返回的 `data.binding` / `data.plan`，或在该工具不可用时手工处理；两种情况下都必须先换算成绝对值。

- "左右各延长 50"，当前 160 mm → `newValue: 260`（不是 210，也不是在旧计划上再累加 50+50）。
- "总长加 50" → `210`；"宽度改为 300" → `300`；"厚度减 2"（当前 10）→ `8`。
- 孔径/半径：用户说直径、而模型参数是半径（或相反）时先换算，并在 `intent.resolved` 与 `reason` 写明换算；无法确认哪个参数是半径就问（§7）。
- 每次换算的基准是**本次快照的当前值**，只换算一次；不要基于历史目标值叠加，也不要"重复执行上一次请求"。

### 5.2 不变量只能写在 `preserve`，且必须接受拒绝

- 用户要求保持孔到边距离时，优先让编译器生成；需要手写时写成
  `preserve:[{"type":"holes_edge_distance","parameter":"<孔位置参数 handle>","targetValue":15.0}]`。
- 如果模型里有多组孔（例如两个安装孔 + 一个中心侧面孔），用户又明确说的是"安装孔/某一组孔"的边距，写成**带作用域**的
  `preserve:[{"type":"hole_edge_distance","sketch":"<草图名>"}]`（也可用 `"holes":[1,2]`，1-based 指向 `snapshot.holes` 的实测孔）。
  - 先用 `snapshot.preserves.hole_edge_distance.scopedGroups` 找该草图组是否 `supported:true`、以及 `preservedBy` 里有没有当前要改的 driver。
  - 全局 `supported:false` 不等于该组不可保持：全局可能因为另一组孔（如中心侧面孔）移动而降级；带作用域的 evidence 只比较选中的孔。
  - 全局 `supported:false` 且用户没有点名孔组时，不得自行缩小范围；只能说明原因并请用户明确是哪一个孔组。
  - 编译器会返回 `preserve-edge-distance-scoped` 检查，max deviation 必须 ≤1e-6 mm；超了就拒绝，不得删 scope 重试。
- 提交前检查 `snapshot.capabilities.invariants.holes_edge_distance.supported`：为 `false` 就不要提交该计划，把 `reason` 原样告诉用户。
- 用 `catia_compile_intent` 时，不可执行的 `preserve` 会直接返回 `decision:"unsupported"`。此时不得删掉该项、改用 `acceptance` 顶替、或声称"边距保持不变"。
- 服务端返回 `INVARIANT_UNSUPPORTED` 是**执行前拒绝**，模型没有改。禁止：删掉 `preserve` 项重试、改用 `acceptance` 顶替、或声称"边距保持不变"。想改变需求（接受边距会变）必须由用户明确同意。
- apply 结果里 `preserve-<index>:holes_edge_distance` 检查失败即视为失败，不得忽略。

### 5.3 `acceptance` 只是断言，不能替代服务端校验

- 只允许 8 种 `type`（白名单，其它一律 `INVALID_PLAN`）：`bbox_range`、`bbox_dimension`、`volume`、`hole_positions`、`hole_world_positions`、`hole_radius`、`unchanged_dimension`、`feature_count`。
- 各类型的字段：
  - `bbox_range`：`axis`（`x`/`y`/`z`）、`min`、`max`
  - `bbox_dimension`：`axis`、`expected`
  - `volume`：`expected`，可选 `absTolerance`、`relTolerance`
  - `hole_positions`：`holes:[{x,y}]`（草图局部，只列实测孔）
  - `hole_world_positions`：`holes:[{x,y,z}]`（模型坐标，只列实测孔；需要 `frame` 可读）
  - `hole_radius`：`expected`，可选 `parameter`（handle）；带 `parameter` 时只断言该参数驱动的圆，凸台圆不会被误当孔
  - `unchanged_dimension`：`parameter`（handle）、`expected`
  - `feature_count`：`expected`
- 默认容差 `absTolerance:0.01`；体积按 `max(0.01 mm³, expected×1e-6)` 判等。
- 参数读回、几何是否真的变化、`preserve` 是否成立由服务端自己检查；`acceptance` 只能加断言。测量不可用（`unavailable`）会让 `verdict` 变成 `INCONCLUSIVE`，不能算通过。

### 5.4 会话期间禁止通用写工具

- 编辑会话处于 `ready`/`executing`/`verified`/`saving`/`recovering`/`unknown` 时，`catia_set_parameter`、`catia_pad`、`catia_hole`、`catia_update_part`、`catia_save_document`、`catia_close_document`、`catia_export` 等通用写工具一律返回 `BUSY`，这是设计，不是故障。
- 不得绕开：不要关文档、不要用通用工具再写一次、不要自己改源文件；只用 6 个 `catia_*_edit` 工具完成改型。

## 6. verdict 与状态处置

状态机：

```text
ready --apply--> executing --PASS--> verified --finalize--> saving --> completed
                     |                  \-- 失败判定（NO_EFFECT/WRONG_EFFECT/INCONCLUSIVE）--> failed
                     \-- 执行异常 --> rolled_back | unknown
unknown（禁止写入）--recover--> rolled_back | unknown
```

`ALREADY_SATISFIED` 不进入写入，会话状态通常不变；它本身不等于 `verified`，只有会话此前已经是 `verified` 时才能 finalize。

计划被服务端拒绝时直接返回（`ok:false`、`effects.modified:false`），模型未改；执行失败才可能已部分修改，用 `effects.modified` 与 `data.recovery` 区分。

| verdict | 含义 | 你能做什么 |
|---|---|---|
| `PASS` | 参数读回=目标值，几何/从属参数确有变化，preserve 与 acceptance 全通过；`businessState:"verified"` | 可以 `catia_finalize_edit` |
| `NO_EFFECT` | 参数改了但没有可测量变化（Update 成功 ≠ 几何正确） | 不得 finalize，不得说"已修改"；报告并检查是否选错参数 |
| `WRONG_EFFECT` | 读回值不符、或 preserve/acceptance 失败、或变化与意图矛盾 | 不得 finalize；原样报 `data.checks`；重读快照后重新判断 |
| `INCONCLUSIVE` | 测量不足或执行异常，无法判定 | 不得 finalize；以 `data.businessState` 和 `data.recovery` 为准处置 |
| `ALREADY_SATISFIED` | 目标值已在位，**未执行任何写入**（`effects.modified:false`） | 不能说成"这次改好了"；不要据此 finalize，除非会话此前已是 `verified` |

关键错误码与处置：

- `UNKNOWN_STATE`：会话/操作状态未知，**禁止**后续写入。先 `catia_recover_edit`（先 `dryRun:true` 评估，再 `dryRun:false`）。得到 `RECOVERY_UNPROVEN` 就保持 unknown 并如实报告。
- `DOCUMENT_CLOSED`：工作副本已关闭或被替换，服务端不会按同名重绑。对同一源文件重新 `catia_begin_edit`（旧会话作废）。注意 `catia_inspect_edit` 可能 `ok:true` 但 `code:"DOCUMENT_CLOSED"`，要看 `data.documentOpen`。
- `OPERATION_CONFLICT`：该 `operationId` 已用于另一个计划。换新 `operationId` 重建计划，并说明前一次结果；不要试图覆盖。
- `STALE_REVISION`：`expectedRevision` 过期。先 `catia_inspect_edit` 或 `catia_get_edit_status` 重读，用新 `modelRevision` 与新 `operationId` 重建；不要重放旧计划。
- `STALE_PLAN`：`oldValue` 与现场不符。重读快照后更新 `oldValue`；若目标值已在位，服务端可能直接返回 `ALREADY_SATISFIED`。
- `EXTERNAL_CHANGE`：模型被绕过工作流改动。停止写入并报告；继续需要新的 `catia_begin_edit` 会话。
- `PARAM_UNKNOWN` / `PARAM_NOT_WRITABLE` / `UNIT_INCOMPATIBLE` / `OUT_OF_RANGE`：计划里的参数或数值不合规。按 code 修计划，不要原样重试。
- `INVALID_PLAN`：入参或计划结构不合法（schema/字段错、`changes` 为空、会话已完成、路径不绝对等）。按 `message` 修正，不要原样重试。
- `INVARIANT_UNSUPPORTED` / `UNSUPPORTED_CAPABILITY`：拒绝。不要降级、不要换工具绕过。
- `BUSY`：别的写入正在进行，或同一源文件已有活跃会话。不要并行施压；按 `data.activeSessions` 说明。
- `CLARIFICATION_PENDING`：上一轮需要用户回答的 `needs_clarification` 还没被回答。先向用户提问；回答后用 §4.1 的 `userConfirmed`/`confirmationText` 重新编译，不要手写计划或反复 apply。
- `AMBIGUITY_OPEN`：上一轮是模型可自行修正的绑定歧义，但还没有一次 `planned`。用精确 handle/sketch/kind 重试 compile；不要手写计划，也不要把它当成用户澄清去提问。
- `SAVE_CONFLICT`：交付路径已存在。换一个**新的**路径，不要覆盖。
- `SAVE_FAILED`：模型已验证但保存失败或保存到了别处。报告；重试 finalize 需换路径或修复环境。
- `VERIFY_FAILED`：磁盘重开验证失败（会话变 `unknown`）。绝不能当作交付成功。
- `RECOVERY_UNPROVEN`：恢复无法证明，状态保持 `failed`/`unknown`；禁止继续写入。
- 另外会见到 `ALREADY_BOUND`（`ok:true`，同源已有会话，复用其 id）、`EXECUTION_FAILED`（执行异常且回滚已被读回证明）、`NOT_FOUND`（会话 id 不存在）。它们同样要原样报告。

## 7. 歧义时先问，澄清前零写入

典型歧义：改哪根轴（长/宽/高、X/Y/Z）；改哪个参数（同尺寸有多个、甚至有公式驱动参数）；单侧还是总尺寸（"各""总共"）；直径还是半径；单位；是否需要保持孔边距。

- `catia_compile_intent` 返回 `decision:"needs_clarification"` 时，按 `data.unresolved` 逐条提问，并把 `data.candidates` 里的 handle + 当前值 + 单位 + 轴/孔径语义列给用户；不要让用户猜参数名。
- 编译器返回 `decision:"unsupported"` 时，把原因原样说明；不要为了"先做点什么"而改一个近似参数。
- 澄清前可以：`catia_begin_edit`、`catia_inspect_edit`、`catia_compile_intent`、`catia_get_edit_status` 读快照。
- 澄清前不得：构造/提交 `changes`、调用 `catia_apply_edit_plan`、调用 `catia_finalize_edit`、使用任何通用写工具。
- 提问时带上从快照找到的候选（`handle` + 当前值 + `unit` + `writable`），让用户确认或选择，不要让用户猜参数名。

## 8. 汇报规则

- 失败要原样给：`code`、`message`、`data.verdict`，以及 `data.checks`/`data.steps` 中的失败项。`ok:false`、`NO_EFFECT`、`WRONG_EFFECT`、`INCONCLUSIVE`、`VERIFY_FAILED` 都不能说成"已修改/已完成"。
- `run completed`、工具调用返回、`ok:true` 都不等于 CAD 已验证。只有 `catia_finalize_edit` 返回 `ok:true` + `data.businessState:"completed"` + `data.verification.passed:true`，才能说"已保存到交付路径并通过磁盘重开验证"。
- 交付汇报必须包含：`data.delivery.path`、`sha256`、`data.verification.checks`，以及改前/改后读回值（`data.before`/`data.after`）。
- 拒绝（计划未执行）与失败（可能已改）必须区分：用 `effects.modified` 说明"模型是否被改过"。

## 9. 多轮状态与案例检索（P2）

### 9.1 多轮基准

- 每次 `catia_compile_intent` 都用当前快照的值；`reference.basis:"current"` 是默认。
- 用户说"再加 10"/"继续加" → 以**当前**值为基准，`increase 10`。
- 用户说"比原始长度加 10"/"在最初模型上" → `reference.basis:"original"`；编译器只用本会话 `begin_edit` 时记录的值，缺失就澄清，不从对话里估算。
- 用户说"恢复上一步"/"撤销上一次" → 先 `catia_get_edit_history` 确认最后一笔是 `PASS` 且 `canRevertLast:true`，再调用 `catia_revert_last_edit(editSessionId, confirm:true)`。它是**新的写入**（新 operationId、revision+1、重新验证）；不要手写反向计划，也不要在 `canRevertLast:false` 时硬撤。
- `catia_get_edit_history` 里的 `operations[]` 是服务端记录，不是模型记忆；汇报时引用其中的 before/after 值与 revision。
- "恢复上一步"只撤最后一笔；连续恢复会被 `ALREADY_REVERTED` 拒绝，不要把它当成"重做"。

### 9.2 案例模板

- `catia_find_cases` 按**结构兼容性**（孔/凸台角色、特征种类、草图平面）和操作类型筛选，不按文本相似度。
- 返回的 `planTemplate` 只有属性、操作、scope、preserve、acceptance 的组合；**没有 handle、旧 revision、旧目标值**。
- 命中案例只用来提醒"这种结构通常怎么改"；仍必须用当前快照重新编译，并重新走 apply/finalize/独立验证。案例的 `verification.passed` 不能替代本次验证。

### 9.3 上下文提炼

- `catia_get_task_context` 是**选择**而不是摘要：handle、值、单位、revision、角色证据、坐标系数值都逐字保留，并给出 `contextDigest` 与完整快照入口 `catia_inspect_edit`。
- 上下文里没有的候选不等于不存在；需要扩大范围时回读完整快照。`limitations[]` 必须读，它说明哪些测量不可用。
