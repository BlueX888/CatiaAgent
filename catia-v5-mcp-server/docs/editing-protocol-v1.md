# CATIA 编辑协议 v1（第一阶段合同）

状态：冻结于 2026-10-23。所有实现、夹具、验收脚本引用 `catia-edit/1`。
本文件只定义机器可判定的内容；不描述 CATIA 的完整能力。

## 1. 单位与数值

- 长度统一 mm，角度统一 deg；参数在快照与计划中一律以规范单位表达。
- 无单位量（real/integer/string/boolean）原样传递，`unit` 为 `"none"`。
- 长度赋值优先 `ValuateFromString("<v>mm")`（实机验证过的路线），并在读回时核对数值。
- 容差：长度/坐标 ±0.01 mm；体积 `max(0.01 mm³, 期望×1e-6)`；除非计划显式给出更严格且可验证的容差。

## 2. 业务状态机

```
ready ──apply──> executing ──> verified ──finalize──> saving ──> completed
  │                  │             │                     │
  │                  ├──> rejected（执行前静态拒绝，模型未改）
  │                  ├──> failed（有失败证据）
  │                  └──> unknown（无法判定是否已改）
  └──> recovering ──> rolled_back | unknown

abandoned：工作副本已被关闭、无法再绑定，会话作废（未发生写入）。
```

- `verified` 只表示当前内存模型通过校验；磁盘重开通过才是 `completed`。
- `rejected` 必须在任何写操作之前返回；`failed` 必须给出是否可能已部分修改。
- `rejected` 是**计划/操作级**结果（`data.businessState="rejected"`，`data.sessionState` 保留会话状态）：一次被拒的计划不会把会话永久锁死，修正后的计划仍可执行。被拒计划不得留下写入痕迹。
- `unknown` 之后禁止该会话的后续写入，直到 `recover` 或新的 `begin_edit` 核对现场。
- `failed`、`rolled_back` 会话在工作副本仍打开时同样保持写入排他；`recover` 在无待办操作时可以关闭该副本并把会话置为 `abandoned`，从而释放排他（`verified` 会话不得被这样关闭）。

## 3. 对象

### 3.1 ModelSnapshot（`catia-model-snapshot/1`）

| 字段 | 语义 |
|---|---|
| `schema` | `"catia-model-snapshot/1"` |
| `editSessionId` | 会话 id（服务端生成） |
| `modelRevision` | 整数，随每次成功 apply 递增；也随外部改动检测而失效 |
| `source` | `{path, sha256, sizeBytes, mtimeUtc}` 原始只读件 |
| `workingCopy` | `{path, sha256, createdUtc}` 服务端工作副本 |
| `document` | `{name, fullName, saved}` |
| `part` | `{name, mainBody, featureCount, bodyNames}` |
| `parameters[]` | 见 3.2，含全部参数 |
| `relations[]` | `{name, expression, targetHandle}`；表达式为 CATIA 原文 |
| `geometry` | `{volumeMm3, areaMm2, bbox:{min,max,dimensions}, method, exact}`；`exact=false` 时不得作为精确证据 |
| `holes[]` | `{center:{x,y,z}, radius, source}`，来自草图圆读回，`source` 标明草图名 |
| `capabilities` | `{writableDrivers[], invariants{holes_edge_distance:{supported,reason}}, unsupported[]}` |
| `limitations[]` | 人类可读的信息缺失声明（如"单位不可用"） |

### 3.2 参数条目

```json
{"handle":"Part2\\零件几何体\\草图.1\\偏移.1","name":"...同上...","value":65.0,
 "unit":"mm","dimensionType":"LENGTH","kind":"dimension|driver|feature|system",
 "writable":false,"drivenBy":{"relationName":"HoleX_L","expression":"L/2-E"},"notes":[]}
```

- `handle` 就是参数全名（本地化名称原样保留）；调用方不得自行拼造。
- `writable=true` 仅当：参数存在、类型为 LENGTH/ANGLE/REAL/INTEGER、非公式目标、非系统只读、且不在从属特征上。
- 公式驱动参数 `writable=false`。V5 的 `Relation` 只暴露表达式、不暴露目标参数，因此 `drivenBy` 恒为 `null`；驱动的判定依据是"该参数不是可写 driver"加上 `dependencies` 实测结果，不伪造关系链。
- 信息缺失时 `unit` 为 `"unknown"`、`dimensionType` 为 `"UNKNOWN"`，不得猜测；`unit="unknown"` 的参数不接受的单位写入（`UNIT_INCOMPATIBLE`）。

### 3.3 EditPlan（`catia-edit-plan/1`）

```json
{"schema":"catia-edit-plan/1","editSessionId":"...","operationId":"<uuid or caller token>",
 "expectedRevision":1,
 "intent":{"text":"长度改为 200 mm","resolved":"absolute length = 200 mm"},
 "changes":[{"parameter":"<handle>","oldValue":160.0,"newValue":200.0,"unit":"mm","reason":"..."}],
 "preserve":[{"type":"holes_edge_distance","parameter":"<handle>","targetValue":15.0}],
 "acceptance":[{"type":"bbox_range","axis":"x","min":-100,"max":100},
               {"type":"volume","expected":239692.63,"absTolerance":0.01}]}
```

- 服务端只用 `changes` 执行写入；`acceptance` 只增加断言，永远不能替代服务端自身的响应性检查。
- `oldValue` 必须与执行时读回值一致（容差 1e-9 相对 / 1e-6 绝对），否则 `STALE_PLAN`。
- `preserve` 必须有模型内可证明的依赖（公式或可识别的边距约束）；否则 `INVARIANT_UNSUPPORTED`，执行前拒绝。
- `acceptance.type` 白名单：`bbox_range`、`bbox_dimension`、`volume`、`hole_positions`、`hole_world_positions`、`hole_radius`、`unchanged_dimension`、`feature_count`。`hole_radius` 可带 `parameter` handle，只断言该参数驱动的圆。

### 3.4 EditResult

复用 `results.operation_result` envelope，并增加：

```json
{"businessState":"verified","editSessionId":"...","modelRevision":2,
 "planHash":"sha256:...","idempotentReplay":false,
 "before":{"parameters":{...},"geometry":{...}},
 "after":{"parameters":{...},"geometry":{...}},
 "steps":[{"index":1,"parameter":"...","oldValue":160.0,"newValue":200.0,"status":"applied","readback":200.0}],
 "checks":[{"id":"param-readback","status":"pass","evidence":{...}},
           {"id":"acceptance-1","status":"pass","evidence":{...}}],
 "verdict":"PASS|NO_EFFECT|WRONG_EFFECT|INCONCLUSIVE|ALREADY_SATISFIED",
 "delivery":null,
 "recovery":{"attempted":false,"restored":null}}
```

## 4. 工具（MCP 名称冻结）

| 工具 | 入参 | 出参 |
|---|---|---|
| `catia_begin_edit` | `sourcePath`、可选 `operationId`、可选 `workingDir` | EditResult + ModelSnapshot |
| `catia_inspect_edit` | `editSessionId` | EditResult + 最新快照（只读） |
| `catia_apply_edit_plan` | `editSessionId`、`plan` | EditResult |
| `catia_get_edit_status` | `editSessionId` | 状态 + 最近结果（只读，不触碰 COM 写） |
| `catia_finalize_edit` | `editSessionId`、`deliveryPath` | EditResult，含交付文件 hash 与重开验证 |
| `catia_recover_edit` | `editSessionId`、`dryRun` | EditResult，含恢复证据 |

所有工具返回结构化 dict（`structuredContent` + JSON 文本），`ok=false` 时 `code` 为下节错误码。
`catia_begin_edit` / `catia_inspect_edit` 返回快照对象；`catia_apply_edit_plan` / `catia_finalize_edit` / `catia_recover_edit` 返回 EditResult 形（`verdict`、`checks`、`recovery` 按语义出现）；`catia_get_edit_status` 只读日志与最近结果。

## 5. 错误码

| code | 含义 |
|---|---|
| `INVALID_PLAN` | schema/字段不合法 |
| `PARAM_UNKNOWN` | handle 不在当前快照 |
| `PARAM_NOT_WRITABLE` | 公式驱动/系统参数/只读 |
| `UNIT_INCOMPATIBLE` | 单位与参数量纲不符 |
| `OUT_OF_RANGE` | 目标值非正或超出声明范围 |
| `STALE_REVISION` | revision 不符 |
| `STALE_PLAN` | `oldValue` 与现场读回不符 |
| `EXTERNAL_CHANGE` | 模型被绕过工作流修改 |
| `INVARIANT_UNSUPPORTED` | 要求保持的依赖在模型内无法证明 |
| `UNSUPPORTED_CAPABILITY` | 目标对象/能力本阶段不支持 |
| `SAVE_CONFLICT` | 交付路径已存在 |
| `SAVE_FAILED` | 保存失败（模型已验证） |
| `REPORT_FAILED` | 交付文件已保存且重开验证通过，但 JSON/Markdown 报告写失败；会话保持 unknown |
| `VERIFY_FAILED` | 磁盘重开验证失败 |
| `EXECUTION_FAILED` | 执行中断，已回滚并经读回证明 |
| `RECOVERY_UNPROVEN` | 恢复无法证明 |
| `OPERATION_CONFLICT` | 同 operationId 不同计划 |
| `UNKNOWN_STATE` | 完成状态未知，禁止后续写入 |
| `BUSY` | 其他编辑写入进行中 |
| `DOCUMENT_CLOSED` | 绑定文档已关闭；需重新 begin_edit |
| `ALREADY_BOUND` | `ok=true`：该源已有可继续的会话，返回其快照 |
| `NOT_FOUND` | 未知会话 id |
| `NOTHING_TO_RECOVER` / `RECONCILED_CLEAN` | 无待对账操作 / 无在飞操作，状态已归位 |
| `VERIFIED` / `COMPLETED` / `ROLLED_BACK` / `ASSESSED` / `RECOVERED` | 正常成功的业务码；`RECOVERED` 表示该 operation 已被恢复流程对账，不会再次执行 |

`modelRevision` 每次**改变模型**的 apply 递增（含未通过校验的写入）；回滚已被证明的失败不递增。

判定码（`verdict`）：`PASS`、`NO_EFFECT`（Update 成功但几何/从属参数无变化）、`WRONG_EFFECT`（变化与意图矛盾）、`INCONCLUSIVE`（测量不足）、`ALREADY_SATISFIED`（已是目标值且不变量成立）。

## 6. 不变量与能力声明

- `holes_edge_distance`：以**实测扰动**为准。驱动参数 +1 后，比较实际移动的实测孔（`role:"hole"`）到最近边的距离；移动孔的边距保持不变才声明 `supported=true`。公式/几何约束只是关联证据，不是能力判定的替代。
- `bbox`：顶点扫描法，仅对平面多面体标 `exact=true`；曲面或顶点数被截断时 `exact=false`。
- 体积：`Measurable.Volume` × 1e9；保存后重开必须复测。

## 7. 身份、排他、恢复

- 会话绑定 `workingCopy.path`（规范化绝对路径，大小写不敏感）；每个工具调用在 STA 内**按路径重新解析**文档，不依赖 `ActiveDocument`。
- 绑定文档关闭/被替换 → `DOCUMENT_CLOSED`，不按同名静默重绑。
- 编辑会话打开期间，通用写工具（参数/草图/特征/保存/关闭等）一律 `BUSY`，避免绕过协调器。
- 每次 apply 前落盘 `intent`，写入后落盘 `readback`；幂等作用域是 **(editSessionId, operationId)**：同一会话内同计划重放返回原结果，不同计划 `OPERATION_CONFLICT`；不同会话可以复用同一个 operationId 而互不干扰。`recovered` 的操作重放同样返回既有结果，不会再次执行。
- 失败恢复只允许一次逆序回写旧值 + Update + 测量；恢复证明必须逐项读回**参数、体积、包围盒尺寸、孔位/半径**，只比较其中一部分不足以宣告 `restored=true`；不可用（模型本身无该证据）的维度记入 `unavailable`，全部维度都不可用时 `restored=false`。不能证明恢复则 `RECOVERY_UNPROVEN`，状态保持 `unknown`。
- 交付落盘后必须实测源文件 SHA-256 写入 `verification.sourceUnchanged`；报告不得用缺省值声称源文件未变。报告写失败时交付文件保留，但会话不得标 `completed`（`REPORT_FAILED`）。
