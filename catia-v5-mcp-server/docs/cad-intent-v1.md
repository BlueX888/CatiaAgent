# `cad-intent-draft/1` 与 `catia_compile_intent`

状态：P0 实验性辅助层。它不修改 `catia-edit/1` 的协议、状态机或写入语义。

## 1. 为什么存在

`catia-edit/1` 能校验一个计划是否合法、执行后是否满足断言，但一个**结构合法、语义错误**的计划仍可能被执行。
典型错误：

- "左右各加 50" 被算成总长 +50，而不是 +100；
- "直径增加 2" 被当成半径 +2（或反之）；
- "比原始长度加 10" 用了当前值而不是原始值；
- 把不可保持的孔边距从 `preserve` 里删掉后强行提交；
- 同名/同轴多参数时随机选一个。

`catia_compile_intent` 只做确定性工作：绑定 handle、换算单位与直径/半径、按 scope 计算绝对目标、检查 preserve 的实测可行性、编译并静态校验计划。
自然语言理解仍由模型完成；模型自报的数值不再直接进入计划。

## 2. 工具

```json
{
  "name": "catia_compile_intent",
  "input": {
    "editSessionId": "<catia_begin_edit 返回的 id>",
    "intent": { "schema": "cad-intent-draft/1", "...": "..." },
    "operationId": "optional",
    "userText": "<host-injected current user message; never fill>",
    "userConfirmed": true,
    "confirmationText": "<the user's reply, verbatim>"
  }
}
```

只读：不写参数、不 Update、不保存。计划仍要交给 `catia_apply_edit_plan`，由既有协调器执行和判定。

## 3. 草稿字段

| 字段 | 取值 | 语义 |
|---|---|---|
| `schema` | `cad-intent-draft/1` | 必填 |
| `text` | string | 用户原话，必须逐字；含"假设/解释为/原话/不含孔"等改写标记会被判为澄清 |
| `decision` | `planned` / `needs_clarification` / `unsupported` | 模型已有判断时可提前声明 |
| `target.parameter` | 快照 handle 或 null | 精确 handle；缺失时返回候选 |
| `target.attribute` | `length` / `width` / `height` / `thickness` / `diameter` / `radius` / `angle` | 用户说的量，不是参数名 |
| `target.binding` | `user_explicit`（可选） | 只有用户点名参数时才写；否则参与歧义检查 |
| `target.holeCount` | 正整数（可选） | 用户提到的孔数量；与快照圆的数量不一致时澄清 |
| `operation.type` | `set` / `increase` / `decrease` | `set` 为绝对目标 |
| `operation.amount` | number | 正数；`set` 时是绝对目标 |
| `operation.unit` | `mm` / `cm` / `in` / `deg` / `none` | 缺失 → 澄清；不猜单位 |
| `operation.scope` | `overall` / `each_side` / `single_side` | `each_side` 总变化为 2×；`single_side` 需方向 |
| `reference.basis` | `current` / `original` | `original` 用会话 begin 时的值；缺失 → 澄清 |
| `reference.revision` | integer | 与快照不一致 → 澄清 |
| `reference.frame` | `model` / `sketch:<name>` | `view` 拒绝；`sketch:<name>` 需要 `directionText` 并映射到模型轴 |
| `reference.axis` | `x` / `y` / `z` | 与实测轴不一致 → 澄清 |
| `reference.direction` | `positive` / `negative` | `single_side` 必填 |
| `reference.directionText` | `left`/`right`/`up`/`down` | 在 `sketch:<name>` 中把方向映射到模型轴 |
| `target.kind` | `hole` / `additive_feature` | 按实测圆角色过滤候选，防止拿孔驱动改凸台 |
| `target.sketch` | sketch name | 限定圆形特征所在草图 |
| `preserve` | `center_position` / `hole_edge_distance` / `hole_centers` / `hole_world_position` / `other_dimensions` | 不可执行时拒绝，不静默丢弃 |
| `unresolved` | string[] | 模型主动澄清时说明缺什么 |

## 4. 实测证据

编译器只使用快照里的 `impacts`（`catia_begin_edit` 在服务端工作副本上对每个可写 driver 做 +1 单位扰动、Update、读回、恢复后得到）：

- `bboxDeltaX/Y/Z`：每单位扰动引起的包围盒尺寸变化，用来判断参数驱动哪根轴、是否 1:1；
- `bboxEdgeDeltas`：每单位扰动引起的 min/max 有符号位移，用来区分"两侧对称"与"单侧锚定"；
- `holeRadiusDelta`：每单位扰动引起的孔半径变化，0.5 表示参数是直径，1.0 表示是半径；`holeRadiusDeltas` 给出逐圆增量，参数只驱动部分圆时用它；
- `holeCentersMoved`：只表示孔心是否移动（半径变化单独记在 `holeRadiiChanged`）；
- `edgeDistancesPreserved`：扰动前后孔到最近边的距离是否不变；
- `geometryChanged`：是否产生可测量变化。

单点 +1 扰动只能证明局部线性；对于由尺寸约束直接驱动、比例 1:1 的参数，编译按线性预测。超出模型有效范围时，由 `catia_apply_edit_plan` 的读回/几何检查拒绝，而不是由编译器放行。

## 5. 返回决策

| decision | `ok` | 含义 | 调用方动作 |
|---|---|---|---|
| `planned` | true | `data.plan` 通过 `validate_plan` | 原样提交计划 |
| `needs_clarification` | true | 缺绑定、单位、方向、原始值或存在多候选 | 按 `unresolved` / `candidates` 提问，零写入 |
| `unsupported` | true | 能力不足、preserve 不可执行、参数不驱动几何等 | 原样说明，零写入 |
| `invalid` | false | 草稿结构错误或编译出的计划未过静态校验 | 修正草稿，不得绕过 |

`data.binding` 给出 handle、当前值、单位、`meaning`（`linear_dimension` / `hole_dimension` / `angle`）、`axis`、`ratio` 和证据；`data.checks` 记录每一步判断。`data.candidates` 只在需要澄清绑定或参数选择时非空。`data.textSource` 说明文本来自 `platform`（host 注入）还是 `model`；真实回合应为 `platform`。

## 5.1 文本来源与澄清闸门

- host 注入 `userText` 时，`intent.text` 必须是它的逐字片段，否则 `text.provenance` → `needs_clarification`。
- `needs_clarification` 分两种：需要**用户回答**时置 `pendingClarification`（含 host 注入的原话、`unresolved`、revision）；模型可自行修正的绑定歧义（缺 handle、需要 sketch/kind 取舍）置 `openAmbiguity` 并返回 `clarificationGate.armed:false`。后者允许用结构化判别字段重试 compile，但 `catia_apply_edit_plan` 返回 `AMBIGUITY_OPEN`，直到有一次 compile 返回 `planned`。`pendingClarification` 存在时：
  - 同一原话再次编译出 `planned` 会被转为 `needs_clarification`；
  - `catia_apply_edit_plan` 返回 `CLARIFICATION_PENDING`（`openAmbiguity` 时为 `AMBIGUITY_OPEN`），手写计划也不能绕过；
  - 只有用户的新消息（与本轮 `userText` 不同）+ `userConfirmed:true` + `confirmationText`（本轮 `userText` 的子串）才能清闸门。
- 清闸门后返回 `data.clarificationCleared` 作为取证。

## 5.2 P1 结构证据与 P2 辅助工具

- 快照新增 `structure`（特征名/材质角色/证据强度、草图 frame/constraints）、`holes[].role`/`roleEvidence`/`centerWorld`、`relationGraph`、`preserves`。
- `catia_get_task_context`：只读，按 `attribute`/`text` 选候选、角色、坐标系、关系和保持能力，保留 `catia_inspect_edit` 完整快照入口。
- `catia_find_cases`：只读，按结构和操作兼容性返回计划模板；不含 handle/旧值。
- `catia_get_edit_history`：只读，多轮状态、原始基准、最近操作。
- `catia_revert_last_edit`：写，恢复上一笔 PASS 的 before 状态，走正常 apply 路径。

## 6. 明确不做

- 不解析自然语言（由模型完成）；
- 不新增特征、不改装配、不处理多实体；
- 不证明公式目标参数（本机 CATIA `Relation` 不暴露目标）；
- 不把特征名标记当作特征类型的实测证明；不把扰动单点证据外推到任意尺寸范围；
- 不替代 `catia_apply_edit_plan` 的服务端校验、执行、读回和独立几何验证；
- 不把 `planned` 当作"已修改"或"已交付"。
