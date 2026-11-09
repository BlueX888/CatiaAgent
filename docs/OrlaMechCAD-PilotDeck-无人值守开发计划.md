# OrlaMechCAD-Nano × PilotDeck 全量打通：无人值守开发文档

- 编写日期：2026-11-06
- 读者：执行开发的低级编码模型（下称"执行者"）。它在无人值守下按本文逐阶段推进。
- 目标：把 `datasets/OrlaMechCAD-Nano` 的全部信息输入 PilotDeck，打通"数据集 → PilotDeck → 模型 → CATIA → 独立验收"整条链路，**14 个案例全部按第 3 节标准通过**。

> 执行者必读：先完整读完第 0–4 节，再开始第 5 节的阶段任务。每次会话开始先读 `runs/orla-pilotdeck/STATE.json` 和 `PROGRESS.md`，从中断处继续。

---

## 0. 铁律（违反任意一条，本轮成果作废）

1. **CATIA 串行。** 同一时刻只允许一个 PilotDeck 回合或一个 CATIA 脚本操作 CATIA。提交回合后，在它到达终态之前**绝不重发**。重发曾导致两个会话交错写同一文档，结果作废。
2. **禁止导入 STEP 冒充建模。** 交付的 CATPart 必须由 `catia_build_part` 用原生特征（Pad/Pocket/Shaft/Groove/Hole/Fillet/Chamfer/阵列等）生成。禁止 `Documents.Open(*.stp)`，禁止用 STEP 导入体、Datum 实体或 Isolate 结果作为交付。
3. **禁止按案例硬编码。** PilotDeck、MCP server、技能、提示词模板里不得出现 `ORL-xxxx`、`part_xxxx` 或任何单个案例的专用分支和专用数值。改进必须是通用的。案例专属信息只能来自第 5.1 节的数据包生成器，而生成器本身也是通用代码。
4. **需求必须由 PilotDeck 里的模型产出。** 执行者不得手写某案例的 requirements JSON 交给 `catia_build_part`，也不得绕开模型直接调 MCP 建模来"凑通过"。
5. **验收容差先冻结。** `acceptance-tolerances.json` 在第一次真实运行前写定，记录 sha256，之后不得修改。见到结果后放宽容差属于作弊。
6. **不改原始数据。** `datasets/OrlaMechCAD-Nano/` 只读。旧目录 `datasets/orla-pdf-modeling/` 和 `runs/orlamechcad-nano/` 只读：可以读、可以 import 函数，不能改。
7. **不提交 git。** `PilotDeck/` 和 `catia-v5-mcp-server/` 是独立仓库，里面有用户未提交的修改。执行者不 commit、不 push、不 reset、不 checkout 覆盖。每个阶段结束时把 `git diff` 存成补丁，路径见第 2 节。
8. **如实报告。** 失败就写失败，未验证就写 `NOT_VERIFIED`。不允许把"回合 completed"当成"建模通过"。
9. **不在根目录建文档。** `F:\CatiaAgent\` 根目录只保留 `项目需求.md`，开发计划和设计文档统一放在 `F:\CatiaAgent\docs\`。
10. **模型固定。** PilotDeck 内使用 `modelbest/deepseek-v4.1-flash-mb`，密钥只从环境变量 `MODELBEST_API_KEY` 读取，不写进任何文件或日志。

---

## 1. 背景事实（已核实，不需要重新调查）

### 1.1 数据集

`F:\CatiaAgent\datasets\OrlaMechCAD-Nano\` 共 14 个零件。每个零件有：

| 资产 | 位置 | 说明 |
|---|---|---|
| 图纸 PDF | `documents/<document_id>.pdf` | 多数 PDF 没有文本层，尺寸是矢量轮廓 |
| DWG/DXF | `drawings/<drawing_id>/` | 另有 `dimensions.json`、`texts.json`、`title_block.json`、`drawing.json`（图元）、`layers.json`、`blocks.json`，以及 `views/` 图纸预览 |
| 参考 STEP | `parts/<part_id>/geometry.step` | 单实体 BRep，单位 mm |
| 面记录 | `parts/<part_id>/faces.json` | 每个面的类型、面积、法向/轴、半径、原点 |
| 测量 | `data/part_measurements.parquet` | 体积、面积、OBB、面类型清单 |
| 零件渲染 | `parts/<part_id>/views/view_00{1..7}.png` | 1 张轴测图加 6 张正交视图，附相机矩阵 |
| 目录 | `data/*.parquet` | 用 `orla_catalog.resolve_case()` 可由 ORL 编号解析出 document/drawing/part |

**尺寸陷阱。** `dimensions.json` 里 `display_text`（图上实际显示的值）与参考 STEP 的尺寸一致；`raw_measurement` 可能差一个比例。例如 FLANGED SLEEVE 显示 `101`，raw 却是 `50.5`；BORED BRACKET 显示 `5080`，raw 是 `101.6`。**以 `display_text` 为准，raw 只作参考。** 显示值是圆整的（如显示 `6`、STEP 实为 `6.3`），这时以 STEP 推导值为准。

**尺度差异大。** 零件包络从 12.7 mm 到 16002 mm，体积从 1.9e3 到 6.4e11 mm³。大尺度会让 OCCT 布尔运算数值不稳，这是历史失败主因之一。

### 1.2 输入边界（用户 2026-11-06 决定）

数据集的**全部**信息都可以给模型，包括 STEP 推导出的几何、`faces.json`、体积/包围盒和零件渲染图。唯一限制是第 0 节第 2 条：交付件必须是原生参数化特征。因此本流程**不是盲测**，报告里要写明这一点。

### 1.3 历史进度（旧 AgentCPM 基座，2026-11-02）

旧基座通过 5/14：`ORL-8ff18328a`、`ORL-21fad5c73`、`ORL-27C07DE36`、`ORL-C3E67E807`、`ORL-A2EFC0244`。这些成绩**不继承**，新链路要全部重跑。其余 9 例的失败原因如下（详见 `datasets/orla-pdf-modeling/README.md` 第 121–146 行）：

| 案例 | 名称 | 历史失败原因 | 本文对策 |
|---|---|---|---|
| ORL-5fc263acb | STEPPED DRILLED SHAFT（9652 mm 长） | 参数编辑副本 OCCT 布尔比较失败；模型流 `network_error` | 5.3 尺度归一化；5.1 数据包减少读图 |
| ORL-44a77937e | RIBBED MOUNTING PLATE | 读图 36 分钟以上未建模，`network_error` | 5.1 数据包直接给出轮廓与尺寸 |
| ORL-b8f4c2a1 | CHAIN SPROCKET（85 面） | 齿形端点写死，OD 参数不驱动外形；编辑比较 inconclusive | 5.3 编辑检查；5.2 技能补充"驱动参数"规则 |
| ORL-e3a7d950 | ACCESS DOOR COVER PLATE | 建成，但验收时找不到对齐；模型调用了不存在的工具 | 5.1 规范坐标系；5.4 验收器 |
| ORL-b6dd4661b | SLOTTED STEPPED SHAFT | 编辑副本比较 inconclusive | 5.3 |
| ORL-b42af6f03 | BORED BRACKET（5081 mm） | 建成，但对齐失败、布尔不确定 | 5.1 规范坐标系；5.3 和 5.4 的尺度归一化 |
| ORL-E7B8F3EB4 | SLOTTED BENT BRACKET | 编辑比较 inconclusive | 5.3 |
| ORL-953cc0d16 | RECESSED FOUR HOLE HUB（16002 mm） | 编辑自评 FAIL | 5.3 |
| ORL-e71d009a2 | NOTCHED MOUNTING PLATE | 从未建模，模型想用台阶近似 | 5.1 轮廓提取；5.2 技能 |

### 1.4 新 PilotDeck 现状（2026-11-05 已迁移）

- 仓库 `F:\CatiaAgent\PilotDeck`，迁移报告见 `PilotDeck/products/catia-agent/MIGRATION-REPORT.md`，务必先读第 5 节和第 8 节。
- 启动：`PilotDeck/products/catia-agent/scripts/start-pilotdeck-migration.cmd`。它固定了 `PILOT_HOME=F:\CatiaAgent\work\pilotdeck-migration-20261105\home`，端口为 3001（server）、18789（gateway WS）、5173（UI）。
- CATIA 插件：`products/catia-agent/plugins/catia-edit/`，含技能 `catia-pdf-model` 和 `catia-parametric-edit`。MCP 工具名为 `mcp__catia__catia_*`。
- 模型不能直接读 PDF。PDF 附件只在上下文里留下占位符和路径，需要用 `read_file` 渲染成图片，或用 `catia_inspect_drawing` 读取。
- 无头提交的现成范例：`work/pilotdeck-migration-20261105/tools/e2e-upload-pdf.mts`。它用 `UploadStore` 上传附件，再对 `ws://127.0.0.1:18789/ws` 发 `submit_turn`，令牌在 `$PILOT_HOME/server-token`，然后读事件流直到 `turn_completed`。
- 会话记录：`$PILOT_HOME/projects/<escaped-project>/chats/<sessionKey>.jsonl`。
- **已失效：** `datasets/orla-pdf-modeling/{prepare_cases,run_cases}.py` 和 `datasets/nist-pdf-benchmark/tools/run_case_pipeline.py` 依赖已删除的 `F:\CatiaAgent\AgentCPM`，不要修复它们，另建新链路。
- 可复用：`orla_catalog.py`（解析案例）、`orla_staging.py` 的文件复制和哈希逻辑、`rigid_compare.py`（24 个旋转加惯性/边框对齐），以及 `catia-v5-mcp-server` 里的 `python -m catia_mcp.cad_support compare` 和 `scripts/independent_verify_part.py`。
- OCCT/build123d 环境：`F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe`。MCP 环境：`F:\CatiaAgent\catia-v5-mcp-server\.venv\Scripts\python.exe`。

### 1.5 MCP 证据机制（扩展点）

- `catia_inspect_drawing` 接收 `drawingContextPath`，文件 schema 为 `catia-drawing-context/1`。校验和 span 生成在 `catia_mcp/drawing/pdf_evidence.py:299` 的 `_load_drawing_context`，会生成 `ctx:dimensions:*`、`ctx:texts:*`、`ctx:titleBlock:*` 证据。
- `catia_mcp/drawing/requirements.py` 的 `_is_context_evidence` 决定哪些 `ctx:` 证据合法。
- 目前**没有** STEP 推导证据类型，第 5.2 节要新增 `ctx:referenceGeometry:*`。

---

## 2. 目录与产物约定

| 用途 | 路径 |
|---|---|
| 本计划及后续开发计划、设计文档 | `F:\CatiaAgent\docs\` |
| 本流程代码（新建） | `F:\CatiaAgent\datasets\orla-pilotdeck\` |
| 案例数据包（生成物） | `F:\CatiaAgent\runs\orla-pilotdeck\packs\<ORL编号>\` |
| 运行记录 | `F:\CatiaAgent\runs\orla-pilotdeck\attempts\<ORL编号>\<attemptId>\` |
| 交付 CATPart | `<attempt目录>\deliveries\part.CATPart`，路径每次唯一且事先不存在 |
| 状态与进度 | `F:\CatiaAgent\runs\orla-pilotdeck\STATE.json`、`PROGRESS.md` |
| 冻结容差 | `F:\CatiaAgent\datasets\orla-pilotdeck\acceptance-tolerances.json` |
| 阶段补丁 | `F:\CatiaAgent\runs\orla-pilotdeck\patches\<阶段>-<仓库>.patch` |
| 草稿、调试输出、一次性脚本 | `F:\CatiaAgent\work\orla-pilotdeck\scratch\` |
| 最终报告 | `F:\CatiaAgent\runs\orla-pilotdeck\FINAL-REPORT.md` |

**所有产物都放在项目目录 `F:\CatiaAgent\` 内**，包括代码、数据包、运行记录、补丁、草稿、日志和截图。不写到 `F:\CodingAgent`、C 盘、桌面或系统临时目录。工具需要临时目录时，把 `TEMP`/`TMP` 指向 `F:\CatiaAgent\work\orla-pilotdeck\tmp\`。

所有路径一律用反斜杠绝对路径传给 CATIA（正斜杠会导致 SaveAs E_FAIL，见迁移报告 7.3）。

`STATE.json` 结构：

```json
{
  "phase": "P3",
  "phaseStatus": {"P0": "done", "P1": "done", "P2": "done", "P3": "in_progress"},
  "toleranceSha256": "<冻结后填>",
  "cases": {
    "ORL-8ff18328a": {"status": "pass|fail|not_run|blocked", "attempts": 2,
                      "lastAttempt": "<attemptId>", "failureClass": "F3", "note": "..."}
  },
  "iteration": 1,
  "passCountHistory": [0, 4, 7]
}
```

`PROGRESS.md` 只追加，每条写：时间、做了什么、结果、下一步。

---

## 3. 通过标准（单案例 PASS 必须全部满足）

验收器 `accept_case.py`（第 5.4 节）对每个 attempt 输出 `acceptance.json`。只有以下各项都为 PASS，案例才算 PASS：

| # | 检查 | 判定 |
|---|---|---|
| A1 | 回合终态 | 会话事件流有 `turn_completed`，且全部 `tool_call_started` 都有对应的 `tool_call_finished` |
| A2 | 构建回执 | 会话中**最后一次成功的** `catia_build_part` 结果满足：`ok:true`、`code:"BUILD_COMPLETED"`、`verification.verdict:"PASS"`、没有 `passed:false` 的检查、`editability.passed:true` |
| A3 | 交付文件 | 回执里的 CATPart 和 STEP 存在，sha256 与回执一致 |
| B1 | 原生特征 | 独立进程重开 CATPart 后：PartBody 恰好 1 个实体；特征树只含允许的原生特征类型；没有导入体或 Datum 实体；用户参数 ≥ 1 |
| B2 | 可编辑 | 来自 A2 的 `editability.passed:true` |
| C1 | 体积 | \|V − V_ref\| / V_ref ≤ `volumeRel` |
| C2 | 材料差 | 对齐后，缺料体积加多料体积 ≤ `symDiffRel` × V_ref |
| C3 | 包络 | 对齐后每轴包围盒差 ≤ max(`lenAbsMm`, `lenRel` × 该轴尺寸) |
| C4 | 特征面清单 | 参考体的每个唯一圆柱面、圆锥面、圆环面（按轴线和半径去重）在生成体中都有匹配：半径差 ≤ max(`lenAbsMm`, `lenRel`×r)，轴线距离 ≤ max(`lenAbsMm`, `lenRel`×包络对角线)，轴向夹角 ≤ `angDeg`。另外，唯一平面数（按法向和偏距去重）要相等 |

C4 用来防止"漏一个小孔但体积仍在容差内"的情况。

**冻结容差**写入 `acceptance-tolerances.json`，P3 完成时冻结，之后不得修改：

```json
{"volumeRel": 0.001, "symDiffRel": 0.002, "lenAbsMm": 0.05, "lenRel": 0.0001, "angDeg": 0.1}
```

**对齐规则。** 先在恒等变换下比较，即模型按 5.1 的规范坐标系建模、参考也变换到规范坐标系。不通过时再用 `rigid_compare.py` 做 24 个正交旋转加惯性/边框对齐的刚体搜索，只允许刚体变换，不允许缩放或镜像。

**尺度归一化。** 做布尔运算前，把两个体统一乘 s = 100 / 参考包络最大边长，容差按同一比例换算，报告里记录 s。

**数据集 PASS（最终目标）。** 在代码冻结状态下跑一次完整批次，14 个案例全部 PASS（第 5.7 节）。迭代期间各案例分散得到的 PASS 只是进度，不算最终结论。

---

## 4. 工作循环与停止条件

```
读 STATE/PROGRESS → 执行当前阶段任务 → 跑本阶段测试 → 达到"完成判据"
→ 存补丁 → 更新 STATE/PROGRESS → 进入下一阶段
```

**卡住时的规则**（任一触发都写 `runs/orla-pilotdeck/BLOCKED.md` 并停止）：

- 同一阶段连续 3 次尝试都无法通过该阶段的完成判据。
- P5/P6 中连续 3 轮全量迭代通过数都没有增加。
- CATIA 进入未知状态（构建超时、COM 卡住、出现模态对话框），按 5.5 的恢复流程处理后仍不健康。
- `MODELBEST_API_KEY` 缺失或连续失效，或者需要用户决策（例如数据集本身的 STEP 和图纸冲突）。

`BLOCKED.md` 写清：卡在哪一步、已尝试的方法、证据路径、需要用户决定什么。

**CATIA 独占。** 运行期间默认本机 CATIA 专供本流程。执行者只能结束它自己拉起的 CATIA 进程（PID 记录在 `STATE.json`），不能结束其他进程。

---

## 5. 阶段任务

### P0 环境与基线

任务：

1. 新建 `datasets/orla-pilotdeck/preflight.py`，检查以下各项并输出 `runs/orla-pilotdeck/preflight.json`：
   - Node 版本在 >=22.13 且 <23 之间（迁移报告 5.1 给出了 Node 路径）。
   - MCP venv 能 `import catia_mcp`；`text-to-cad-env` 能 `import build123d, OCP`。
   - `MODELBEST_API_KEY` 存在（只记录是否存在，不记录值）。
   - CATIA 可以 COM 连接，通过 MCP 的 `catia_diagnose` 或 `scripts` 下的健康脚本检查。
   - 3001/18789/5173 端口上运行的是 PilotDeck 新栈，且 `$PILOT_HOME/server-token` 存在。
   - `$PILOT_HOME/runs/registry.json` 等运行记录的大小：大于 10 MB 时**只报警**，不删除、不裁剪。
   - `PILOT_HOME` 必须是迁移用的独立目录，**不能**是默认的 `C:\Users\Admin\.pilotdeck`。默认目录的 `mcp.json` 里有一个同名 `catia` 条目，它会覆盖插件配置，导致缺少 `CATIA_MCP_WORK_DIR`、`CATIA_CAD_SUPPORT_PYTHON`、`CATIA_OCR_PYTHON` 和 30 分钟的调用超时。检查当前 `PILOT_HOME/mcp.json` 中没有 `catia` 条目；有则报错停止，**不要**修改用户默认目录里的文件。
2. 用现成的 `e2e-upload-pdf.mts` 跑一次 DP-01 冒烟测试。它会走真实模型和真实 CATIA，跑之前先确认没有其他回合在运行。

完成判据：`preflight.json` 全部为 ok；DP-01 冒烟交付并通过 `independent_verify.py`。

### P1 案例数据包生成器（核心："充分输入"）

新建 `datasets/orla-pilotdeck/build_case_pack.py`，参数为 `--case <ORL编号>` 或 `--all`，输出到 `runs/orla-pilotdeck/packs/<ORL>/`。需要几何计算的部分放在 `pack_geometry.py`，用 `text-to-cad-env` 的 Python 以子进程方式运行。

每个数据包包含：

| 文件 | 内容 | 作为上传附件？ |
|---|---|---|
| `drawing.pdf` | 原 PDF | 是 |
| `drawing-context.json` | `catia-drawing-context/1`：`pdfSha256`、`assetId`、`sources[]`（每个 kind 的 sha256）、`dimensions`、`texts`、`titleBlock`、`geometry`、`layers`、`blocks`，**外加 5.2 新增的 `referenceGeometry`** | 是 |
| `reference-canonical.step` | 参考 STEP 变换到规范坐标系后的结果，**只读，只给验收器用** | 否，不给模型 |
| `reference-geometry.json` | STEP 推导的几何事实（规范坐标系），见下方 | 是 |
| `brief.md` | 给模型读的人类可读摘要，见下方，控制在约 8k token 以内 | 是 |
| `drawing-preview.png` | 图纸预览（`drawings/<id>/views/view_001.png`） | 是 |
| `part-iso.png` | 零件轴测图（view_001） | 是 |
| `views/` | 其余 6 张正交视图，在 brief 里列出路径，模型需要时用 `read_file` 读 | 否 |
| `pack-manifest.json` | 每个文件的来源路径、sha256、生成器版本，以及变换矩阵 T | 否 |

**规范坐标系**（通用算法，单元测试覆盖）：

1. 判断旋转体：最大面积圆柱的总面积 > 平面总面积，并且多数圆柱共轴时，Z = 该公共轴线，原点放在轴线上、最小 Z 处。
2. 否则：Z = 面积最大平面的法向（指向材料外侧）；X = 与 Z 正交的平面中面积最大者的法向；没有这样的平面时用 OBB 轴。Y = Z × X，保证右手系。
3. 原点放在规范坐标系下轴对齐包围盒的最小角，旋转体按第 1 步处理。
4. 变换 T 记入 `pack-manifest.json`。用 OCP 对参考 STEP 施加 T，写出 `reference-canonical.step`，再复核体积不变（相对差 < 1e-9）。

**`reference-geometry.json`**（schema `orla-reference-geometry/1`，所有坐标在规范坐标系，单位 mm）：

```json
{
  "schema": "orla-reference-geometry/1",
  "partId": "...", "stepSha256": "...", "canonicalTransform": [[...4x4...]],
  "summary": {"volumeMm3": 0, "surfaceAreaMm2": 0, "bboxMm": [[0,0,0],[0,0,0]],
              "faceCount": 0, "isRevolved": false},
  "items": [
    {"id": "rg:cyl:1", "kind": "cylinder", "role": "hole|boss|unknown",
     "radiusMm": 0, "axisPoint": [0,0,0], "axisDir": [0,0,1],
     "axialRangeMm": [0, 0], "faceIds": ["..."], "through": true},
    {"id": "rg:plane:1", "kind": "plane", "normal": [0,0,1], "offsetMm": 0, "areaMm2": 0},
    {"id": "rg:cone:1", "kind": "cone", "...": "..."},
    {"id": "rg:torus:1", "kind": "torus", "...": "..."},
    {"id": "rg:profile:1", "kind": "planar_profile", "plane": "xy", "offsetMm": 0,
     "loops": [[{"type": "line", "start": [0,0], "end": [0,0]},
                {"type": "arc", "center": [0,0], "radius": 0, "start": [0,0], "end": [0,0], "ccw": true}]]},
    {"id": "rg:section:1", "kind": "revolve_section", "axis": "z",
     "segments": [{"type": "line", "start": [0,0], "end": [0,0]}]}
  ]
}
```

规则：

- `role` 的判定：圆柱面法向指向轴线（材料在外侧）为 hole，否则为 boss；判不出写 unknown。
- `through` 的判定：孔的轴向范围覆盖该方向包络的全长。
- `planar_profile`：对每个平行于规范坐标平面、面积 ≥ 总面积 1% 的平面，取外环和内环的边，转成直线段和圆弧段。B 样条段写为 `{"type":"spline_unsupported"}`，不要近似。
- `revolve_section`：只对旋转体生成。用过轴平面截出半截面轮廓。
- 生成器不得读取任何 ORL 编号的专用配置。

**`brief.md` 模板：**

```markdown
# 案例 <ORL编号> <标题>
单位：mm（STEP 声明）。图纸声明单位：<declared_unit>。
本任务提供参考几何（非盲测），但交付必须是原生参数化特征，禁止导入 STEP。

## 包络与体积（规范坐标系）
bbox: ... ; 体积: ... ; 旋转体: 是/否

## 图纸尺寸（display_text 为准）
| ctx id | 显示 | 类型 | 视图/坐标系 |
...（全部尺寸，按 spatial_frame_id 分组）

## 参考几何要点
- 孔/凸台：rg:cyl:N  Ø..  轴..  范围..  贯穿..
- 主要轮廓：rg:profile:N（plane, offset, 段数），详见 reference-geometry.json
- 圆角/倒角：rg:torus / rg:cone ...

## 显示值与 STEP 不一致的尺寸
| ctx id | 显示 | STEP 推导 | 采用 |
（显示值是圆整值时采用 STEP 值）

## 附件
drawing.pdf / drawing-context.json / reference-geometry.json / drawing-preview.png / part-iso.png
其余视图：<绝对路径列表>
```

"显示值与 STEP 不一致"表的生成方法：对每个线性、直径或半径尺寸，在 reference-geometry 中找最接近的候选值（直径对应圆柱 2r，线性尺寸对应平面间距）。相对差 ≤ 1% 但不等于显示值时列入。匹配不上的只列出，不猜。

测试（`test_case_pack.py`）：

- 14 个数据包全部生成；`drawing-context.json` 能通过 MCP 的 `_load_drawing_context` 校验（在测试里直接 import 调用）。
- `reference-canonical.step` 体积与 parquet 中的体积相对差 < 1e-9。
- 圆柱数、平面数和 `part_measurements.surface_inventory` 一致（去重前后各核一次）。
- 用合成体做规范坐标系测试：已知旋转的方块和圆柱，求得的 T 能还原。

完成判据：14 个数据包生成，测试全绿。人工抽查 3 个 `brief.md`（选最小的、最大的和链轮），确认没有超长、尺寸表完整。

### P2 MCP 与技能扩展

**P2-A `ctx:referenceGeometry` 证据**（`catia-v5-mcp-server`）：

1. `_load_drawing_context` 接受可选的 `referenceGeometry` 数组，此时 `sources` 里必须有 `kind:"referenceGeometry"` 的 sha256。数组和哈希要么同时存在，要么都不存在（与 blocks/layers 的规则一致）。
2. 为每个 item 生成 span：`id = "ctx:referenceGeometry:<item.id>"`，`text` 为该项数值的规范字符串（如 `"Ø12.6"`、`"R3"`、`"offset 45"`），`source:"drawing_context"`，`sourceKind:"referenceGeometry"`，同时带上数值字段。
3. 在 `requirements.py` 的 `_is_context_evidence` 中接受 `sourceKind == "referenceGeometry"`，校验 id 格式、sha256 和数值字段的有限性。需求中的数值必须与被引用 span 的数值一致（复用现有"span 必须包含该数字"的检查方式，按 `lenAbsMm` 精度比较）。
4. `catia_inspect_drawing` 增加 `referenceGeometryQuery:{kind, offset, limit}`，用于分页读取。默认回复只给摘要，避免撑爆上下文。
5. 测试：合法引用通过；篡改 sha、伪造 id、数值不符时被拒；没有 `referenceGeometry` 的旧上下文行为不变。旧测试全部保持通过。

**P2-B 技能更新**（`PilotDeck/products/catia-agent/plugins/catia-edit/skills/catia-pdf-model/SKILL.md`）：

新增一节"参考几何辅助模式"，要点如下：

- 附件里有 `brief.md` 和 `reference-geometry.json` 时进入此模式。先读 `brief.md`，**不要先逐页 OCR 读图**。只在 brief 不足时才用 `read_file` 或 `catia_crop_drawing` 核对。
- 以规范坐标系建模，原点和轴向按 brief 中的定义。
- 证据优先级：`ctx:dimensions` 的显示值，其次是 `ctx:referenceGeometry`（显示值为圆整值或缺失时）。PDF/OCR span 只作补充。
- 平面类零件优先用 `planar_profile` 的直线和圆弧段作为拉伸草图；旋转体优先用 `revolve_section` 作为 Shaft 草图。孔、圆角、倒角按 `rg:cyl`/`rg:torus`/`rg:cone` 补齐。
- **驱动参数规则：** 声明为参数的尺寸必须真正驱动几何。例如外径参数要驱动齿顶圆，不能只改某段圆弧半径而端点写死。确实无法参数化的段用常量，不要声明为参数。
- 禁止台阶近似、禁止省略标注过的特征、禁止导入 STEP。
- 不存在的工具不要调用：以本轮工具列表为准，例如本模式不使用 `catia_verify_part`。
- 校验返回 `needs_clarification` 时，本模式不向用户提问，而是在 `reference-geometry` 中找依据补全后重新校验；仍然不足就按失败结束，并写明缺什么。

**P2-C 无人值守提示词模板**：`datasets/orla-pilotdeck/prompt-template.md`。内容只包含通用指令，加上 `{deliveryPath}` 和附件列表占位符，不写任何案例数值。

完成判据：MCP pytest 没有新增失败（基线：773 pass / 1 已知 fail）；新增测试通过；保存补丁。

### P3 独立验收器（先于任何真实运行）

新建 `datasets/orla-pilotdeck/accept_case.py`，参数：`--attempt <attempt目录> --pack <pack目录>`。

1. **A1–A3：** 解析该 attempt 保存的会话 jsonl 和事件日志（由 5.5 的运行器保存），找到最后一次成功的 `catia_build_part` 结果并逐项检查，对交付文件做哈希。
2. **B1：** 调用 `catia-v5-mcp-server/scripts/independent_verify_part.py`，或新写一个只读脚本，在独立 CATIA 会话中重开 CATPart，枚举实体和特征类型。允许的类型白名单写在代码常量里，导入体或 Datum 实体一律拒绝。必要时让 CATPart 重新导出 STEP 到 attempt 目录，作为 C 项比较的输入（不信任模型给的 STEP）。
3. **C1–C4：** 用 `text-to-cad-env` 子进程比较 `reference-canonical.step` 与生成的 STEP。先做恒等比较，不通过再调用 `rigid_compare.py`。布尔运算前按第 3 节做尺度归一化。C4 的面清单匹配要自己实现（读两边 STEP 的解析面）。
4. 输出 `acceptance.json`：每项 `PASS|FAIL|NOT_VERIFIED`、实测值、容差、对齐变换、尺度因子 s、容差文件 sha256。只有全部为 PASS 时，总体才是 PASS。

**验收器自测**（`test_accept_case.py`，不需要模型）：

- 用参考 STEP 自己比较：C 项全部 PASS。
- 对参考体做已知刚体旋转加平移：通过对齐后 PASS。
- 删除一个小孔（用 OCP 填孔）：C4 FAIL。
- 等比放大 1.01 倍：C1 FAIL。
- 对 16002 mm 的轮毂和 9652 mm 的轴做自比较：不得出现 inconclusive，以此检验尺度归一化。
- B1：用迁移时 DP-01 的交付件（`work/pilotdeck-migration-20261105/artifacts/`）跑原生特征检查，应 PASS。另用 CATIA 打开一个 STEP 再另存为 CATPart，作为负例，应 FAIL。负例文件放在 scratch 目录。

完成判据：自测全绿。然后**冻结** `acceptance-tolerances.json`，把 sha256 写入 `STATE.json`。

### P3.5 构建内编辑检查的数值鲁棒性（`catia-v5-mcp-server`）

历史上有 5 例卡在 `catia_build_part` 的参数编辑副本几何自比较（`CAD_SUPPORT_EVALUATION_FAILED / MaterialComparisonInconclusive`）。相关位置：`catia_mcp/drawing/feature_workflow.py`、`catia_mcp/cad_support.py`。

1. **复现：** 在 `work/orla-ui-bracket-20261102/.catia-pdf-runs/*/` 中找历史编辑副本的 STEP 和期望几何，在 `cad_support` 层离线复现，不需要 CATIA。找不到时，就用 P1 的大尺度参考体构造等价测试。
2. **修复：** 在 `cad_support` 的比较入口统一做尺度归一化（同第 3 节），容差同步换算，s 记入回执。
3. 修复后仍然 inconclusive 时，允许退回"测量判定"：用 CATIA SPA 实测体积和包围盒，与期望值的变化方向和幅度比较，回执里标 `method:"measurement_fallback"`。不允许直接判 PASS 而不给任何测量依据。
4. 测试：大尺度体的自比较和编辑比较不再 inconclusive；旧测试不退化。

完成判据：离线复现用例转为确定结论（PASS 或 FAIL，不再是 inconclusive）；pytest 无新增失败；保存补丁。

### P4 无头运行器

新建 `datasets/orla-pilotdeck/run_case.mts`（以 `e2e-upload-pdf.mts` 为蓝本）和 `run_batch.py`。

`run_case.mts --pack <dir> --attempt <dir>`：

1. 获取全局锁 `runs/orla-pilotdeck/catia.lock`（操作系统独占锁，拿不到就退出）。
2. 前置健康检查：CATIA 可连接，没有其他回合运行中（通过 `read_session_messages` 或 gateway 状态确认）。
3. 新建会话（`new_session`），项目根设为 attempt 目录，避免在数据集目录下生成 `.pilotdeck/`。
4. 用 `UploadStore` 上传数据包附件（见 P1 表格的"作为上传附件"列）。
5. 按 `prompt-template.md` 渲染提示词，`deliveryPath` = `<attempt>\deliveries\part.CATPart`。
6. `submit_turn`：`canPrompt:false`、`modelSelection` 固定为 deepseek、`timeoutMs` 为 90 分钟、`maxTurns` 为 80。这里用了 PilotDeck 的 `mode:"bypassPermissions"`（与范例相同），因此只允许在 attempt 目录内写入，提示词中写明这一点。
7. 流式读取事件，逐条写入 `<attempt>/events.jsonl`。到 `turn_completed` 或超时为止。**超时时只发一次 `abort_turn`，然后等待终态，不重发。**
8. 复制会话 jsonl 到 attempt 目录，写 `run.json`：会话 key、起止时间、终态、token 用量、工具调用计数和配对情况。
9. 释放锁。

`run_batch.py --cases all|<ORL,...> [--max-attempts N]`：串行对每个案例调用 `run_case.mts`，再调用 `accept_case.py`，更新 `STATE.json`。以下情况停止整批：

- 上一个回合终态不明（没有 `turn_completed`，或有未配对的工具调用）。
- 健康检查失败。
- 构建返回 `catiaOutcomeUnknown` 或 MCP 围栏错误。

处理方式见下方的 CATIA 未知状态恢复。

**CATIA 未知状态恢复：**

1. 等 5 分钟，确认会话已关闭、事件流已终结。
2. 运行只读健康探测（`work/catia-health-probes/` 有先例），检查 CATIA 是否响应、能否完整枚举文档、是否有模态对话框。
3. 健康就继续。不健康时，如果 CATIA 进程是本流程拉起的，就关闭它（`Quit`，失败再 kill 记录在案的 PID），然后重启 PilotDeck 新栈，再做一次健康探测。
4. 仍不健康时写 `BLOCKED.md` 并停止。

已知陷阱：

- `registry.json` 过大会让提交延迟数分钟，这不是失败，耐心等待事件，**不要重发**。
- 交付路径必须事先不存在，否则会返回 `SAVE_CONFLICT`。
- 模型流 `network_error: terminated`：记为 F1（见 P5），在新会话中重试。

测试：用 mock gateway（本地 WS 假服务）测超时 → abort → 等终态、锁互斥、未配对工具调用时停止批次。真实冒烟：用 DP-01 跑一次 `run_case.mts`，再跑 `accept_case.py`（DP-01 没有 C 项参考，可只跑 A/B 项）。

完成判据：mock 测试全绿，DP-01 真实冒烟 A/B 项 PASS。

### P5 首轮全量与分诊

先跑最简单的 3 例（`ORL-C3E67E807`、`ORL-27C07DE36`、`ORL-A2EFC0244`）。三例都 PASS 后，再执行 `run_batch.py --cases all --max-attempts 2`。

对每个失败的 attempt，按下表分类，写入 `runs/orla-pilotdeck/triage-<iteration>.md`：

| 类 | 现象 | 修复方向 |
|---|---|---|
| F1 模型流中断 | `network_error`、空回复、超 maxTurns | 缩短 brief、减少附件图片、在技能里强调"先读 brief"；新会话重试 |
| F2 读图过久或未建模 | 很多次读图/裁图后没有调用 validate | 技能流程；brief 中轮廓和尺寸的完整度 |
| F3 需求校验不过 | `invalid`/`unsupported`/`needs_clarification` | 若是 schema 能力缺口（槽、倾斜面、阵列、盲孔等），扩展 `feature_schema.py`/`requirements.py`，要通用，并配测试 |
| F4 构建失败 | `catia_build_part` 失败 | MCP 构建器 bug，修复并配测试 |
| F5 编辑检查失败 | `editability` 未通过 | 驱动参数规则（技能）或 P3.5 数值问题 |
| F6 原生性失败 | B1 FAIL | 查明模型行为，加强技能约束 |
| F7 几何不符 | C1–C4 FAIL | 看差异：漏特征、尺寸取值错、坐标系错；改技能、brief 生成规则或 reference-geometry 提取 |
| F8 环境 | CATIA、gateway、密钥 | 按恢复流程处理；无法恢复则 BLOCKED |

### P6 迭代修复

循环：选通过率影响最大的失败类 → 做**通用**修复（代码、技能或生成器）→ 跑相关单元测试 → 重新生成受影响的数据包 → 只重跑失败案例 → 更新 STATE。

每一轮：

- 跑完相关测试，保存补丁，把通过数追加进 `passCountHistory`。
- 修复后**回归**：改动 MCP 构建器、schema 或技能后，已通过的案例中至少挑 2 个重跑，防止退化。
- 每个案例累计 attempt 达到 6 次仍失败时，写一份案例专项分析 `analysis-<ORL>.md`，列出差异证据和怀疑的根因，然后继续处理其他案例。
- 停止条件见第 4 节。

预判的能力缺口（遇到时优先查这里）：

- 链轮 `ORL-b8f4c2a1`：齿形段多（约 78 段），要用参数化的齿顶圆、齿根圆和节圆生成齿形。必要时给 schema 增加通用的"圆周阵列草图段"或 `circ_pattern` 特征。
- 弯折支架 `ORL-E7B8F3EB4`、角支架 `ORL-C3E67E807`：用 L 形截面拉伸，**不是**钣金折弯。
- 带肋/槽类零件：多个 Pad/Pocket 组合加槽（长圆孔）轮廓。
- 倾斜平面（`ORL-8ff18328a` 有法向约 4.8° 的面）：使用"固定倾斜坐标系支撑"。

### P7 定版认证

1. **代码冻结：** 记录 PilotDeck、MCP 和 `datasets/orla-pilotdeck` 的 diff sha256，写入 `STATE.json`。
2. **重新生成全部数据包**，再执行 `run_batch.py --cases all --max-attempts 1`。每例只有一次机会，14/14 PASS 才算数据集通过。不通过就回到 P6，冻结作废。
3. **UI 认证**（用户偏好真实界面路径）：用 ego-browser（派 `user-sim` 子代理）打开 http://127.0.0.1:5173，逐案例上传同一套附件、粘贴同一提示词、点击发送，等待完成后用 `accept_case.py` 验收。UI 的 attempt 单独计数。ego-browser 不可用时，在报告中写明"UI 认证未执行"，无头认证结论不受影响，但不能声称 UI 已通过。
4. **最终报告** `runs/orla-pilotdeck/FINAL-REPORT.md`，包含：
   - 14 例结果表：ORL、名称、PASS/FAIL、attempt 路径、交付 CATPart、sha256、C1–C4 实测值、对齐方式。
   - 冻结容差及其 sha256；代码 diff sha256；所用模型；总 token 和耗时。
   - 声明：本结果使用了参考几何辅助，不是盲测；容差是运行前冻结的诊断阈值，不是工程公差。
   - 所有修改文件的清单，以及补丁路径。
   - 遗留问题和未验证事项。

---

## 6. 执行者自检清单（每阶段结束时逐条核对）

- [ ] 没有在 PilotDeck、MCP 或技能里写入任何 ORL 编号或单个案例的数值。
- [ ] 没有修改 `datasets/OrlaMechCAD-Nano/`、`datasets/orla-pdf-modeling/` 或 `runs/orlamechcad-nano/`。
- [ ] 没有 commit 或 push，补丁已保存。
- [ ] 本阶段所有产物都在 `F:\CatiaAgent\` 内，项目外没有新文件。
- [ ] 测试结果与基线相比没有新增失败；如有，已写明原因。
- [ ] `STATE.json` 和 `PROGRESS.md` 已更新。
- [ ] 没有同时运行两个 CATIA 操作，也没有重发未终结的回合。
- [ ] 报告里的每个 PASS 都有 `acceptance.json` 支撑。

## 7. 参考文件索引

| 内容 | 路径 |
|---|---|
| 迁移报告（启动、限制） | `PilotDeck/products/catia-agent/MIGRATION-REPORT.md` |
| 无头提交范例 | `work/pilotdeck-migration-20261105/tools/e2e-upload-pdf.mts` |
| PDF 建模技能 | `PilotDeck/products/catia-agent/plugins/catia-edit/skills/catia-pdf-model/SKILL.md` |
| 插件与 MCP 配置 | `PilotDeck/products/catia-agent/plugins/catia-edit/plugin.json`、`products/catia-agent/config/pilotdeck.yaml` |
| 上下文校验 | `catia-v5-mcp-server/catia_mcp/drawing/pdf_evidence.py:299`、`requirements.py`（`_is_context_evidence`） |
| 特征 schema | `catia-v5-mcp-server/catia_mcp/drawing/feature_schema.py` |
| 构建与编辑检查 | `catia-v5-mcp-server/catia_mcp/drawing/tools.py`、`feature_workflow.py`、`cad_support.py` |
| 旧 Orla 工具（只读复用） | `datasets/orla-pdf-modeling/orla_catalog.py`、`orla_staging.py`、`rigid_compare.py`、`compare_delivery.py` |
| 旧 Orla 历史与失败细节 | `datasets/orla-pdf-modeling/README.md` |
| 旧上下文样例（`catia-drawing-context/1`） | `runs/orlamechcad-nano/prepared/*/*/generation/drawing-context.json` |
| 历史各案例提示上下文 | `work/orla-ui-bracket-20261102/context-ORL-*.md` |
| 健康探测先例 | `work/catia-health-probes/` |
