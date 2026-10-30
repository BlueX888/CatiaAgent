# 2026-10-30 批次报告：NIST 推进到 2/17，派生图纸与验收链路重构

日期：2026-10-30
工作区：`F:/CatiaAgent`。本批次为无人值守开发，用户要求“暂时收尾”时停止。
**结论：NIST 17 例中 2 例完成两次最终运行验收（FTC-11、FTC-09）；STC-09 完成 1/2；其余 14 例未完成。**

## 1. 指标

| 指标 | 要求 | 本批次 |
| --- | --- | --- |
| 完成两次最终运行验收的案例 | 17 | **2**（FTC-11、FTC-09） |
| 完成一次最终运行的案例 | — | **3**（另加 STC-09） |
| 有充分派生图纸的案例 | 17 | 0（10 个 FTC/CTC + 5 个 STC 有派生排表，但仅部分几何可完整表达） |
| 错误交付（部分完成/未验证报成功） | 0 | 0 |
| 回归 | — | 主环境 403 passed / 2 skipped / 1 xfailed；OCCT 37 passed |

## 2. 完成的案例

### FTC-09（2/2，完成）

- 输入：`work/derived-20261029/FTC-09-layer-v7/FTC-09.pdf`（派生层排表，文本层 + SEG 数组）。
- 运行 1（隔离，五工具）：`work/isolated-generation/7800fba6-.../`，`generated_verified`；
  交付 `runs/nist-pdf-benchmark/20261030/FTC-09/run-isolated-1/part_r2.CATPart`。
- 运行 2（真实 UI 上传）：会话 `web-s_afc07b84-...`，交付
  `runs/nist-pdf-benchmark/20261030/FTC-09/run-ui-2/FTC09_Plate_R3.CATPart`。
- 两次均通过独立重开与 **原始 STEP** 材料比较（fuzzy 布尔）：
  缺失 3.55 mm³ / 多余 1.75 mm³（容差 10 mm³），bbox 误差 4e-5 mm。
- 证据：各 run 目录的 `acceptance.json`（verdict PASS、verification PASS、comparison PASS）、
  `results.json`。

### STC-09（1/2）

- 输入与 FTC-09 相同（NIST README-240318：STC 为 FTC 的 1:1 简化等价件，几何相同），
  在 `verification/STC-09.json` 中记录了等价来源。
- 运行 1（隔离）：`work/isolated-generation/295e2b3e-.../`，`generated_verified`，
  独立验收 PASS（`runs/nist-pdf-benchmark/20261030/STC-09/isolated-1/acceptance.json`）。
- 运行 2（真实 UI）：**未执行**（收尾停止）。

### FTC-11

沿用 2026-10-28 的两次运行（UI 上传 + 同会话批量），本批次未重跑。

## 3. 未完成案例与具体阻塞

| 案例 | 状态 | 具体阻塞/证据 |
| --- | --- | --- |
| FTC-08 | 未完成 | 截面堆栈图纸（16 pad + 30 cut，数据准备重建误差 0.011%）；Agent 生成的需求通过 validate/compile 并建出模型，但 editability 在半径参数上失败；已保存模型的参考差约 −1995/+2056 mm³（0.4%），超过 100.7 mm³ 容差。收尾时停在重试中，未验收 |
| FTC-10 | 未完成 | 构建与工具自检通过，参考差 −221 mm³（0.117%）：漏掉水平圆角/细小特征；当前语言没有把环面/锥面圆角表达成 FILLET 标注 |
| CTC-01 | 未完成 | 首轮 Agent 模型缺层（体积 −91k mm³）；截面排表数据准备重建误差 0.034%（锥面按弦近似），已冻结 0.05% 容差，未重跑 |
| CTC-03 | 未完成 | 截面重建误差 0.59%（侧向特征未进入单轴截面） |
| FTC-06/07 | 未完成 | 截面重建误差 0.118% / 重建失败；大量环面、球面、锥面圆角 |
| CTC-02/04/05 | 未完成 | 截面重建失败（闭合/圆弧求解），多轴与大量混合曲面 |
| HTC | 未开始 | 参考 CATPart 为 V5-6R2023，本机 B31 打不开；NX/Creo 无内核；原始 3D PDF 无内嵌 U3D/PRC。已 OCR 出 11 页尺寸（`work/derived-20261029/HTC-ocr.json`）与 37 页 PPTX 特征说明，但没有可测量参考 |

**共性根因**：这些零件普遍带多轴侧向特征和曲面圆角/倒角；当前组合语言只有沿三主轴的 pad/pocket/shaft/groove，
圆角/倒角只支持几何选择器命中的单条边，截面堆栈只能用弦近似锥面截面。要完整通过，需要：
1. 从参考几何推导“锐边 + 圆角/倒角尺寸”的 FILLET/CHAMFER 标注，并在锐化实体上建截面；
2. 多轴侧向特征的显式表达（侧向 pad/切除，而不只是圆柱）；
3. HTC 的可测量参考或经独立审查的尺寸链。

## 4. 本批次基础设施与契约变化

### 4.1 模型与运行环境

- 生成模型改为 **Claude Opus 5.5**（provider `modelbest-anthropic`，Anthropic 协议，
  `timeoutMs=900000`，`maxOutputTokens=32768`，thinking medium）。原 DeepSeek flash 无法稳定产出大型结构化需求。
- Gateway 工具结果预览上限 20,000 → **200,000** 字符；否则多页排表对模型不可见。
- `run-prepared-generation.mjs`：回合超时 90 分钟；报告查找支持任意 `*.build.json`；
  `PILOT_HOME` 与 `--import tsx` 为运行前提。
- `catia_build_part` 失败返回新增 `failure.stage/reason`，去掉巨型 scene；参数超过 12 个时按固定间隔抽样做改值验证；
  参数扰动改为 2%/最小 0.05 mm；比较容差按期望体积 0.01% 自适应。
- `FeatureExecutor`：PartNumber 冲突时回退到带进程后缀的名字。

### 4.2 需求语言

- 草图坐标允许直接数值（主要尺寸仍必须是带证据的命名参数）。
- 支持紧凑数组轮廓：`["circle",x,y,r]`、`["rectangle",...]`、`["polygon",[[x,y],...]]`、
  `["wire",[x,y],[["line",x,y],["arc",x,y,r,cw,large]]]`。
- 新增顶层 **命名轮廓表** `profiles: {"P1": ...}`，feature 的 `sketch.profiles` 可写 `"P1"`。
  FTC-08 的最小需求从无法整段输出降到约 23 KB（≈5.9k tokens）。

### 4.3 派生图纸工具（data prep）

- `generate_layer_schedule.py`：层法向、轮廓、EXTRUDE/CHAMFER(K) 行、START/SEGn 可复制数组、文本层。
- `generate_section_schedule.py`：按主轴取所有高度层，逐区间输出真实截面（L 行）与独立切除（C 行），
  侧向圆柱/锥/球/环列为 S 行；圆角采样可配；相同截面相邻层合并；锥面截面按弦闭合。
- `text_layer.py`：给 cadgen 的矢量图纸叠加逐行一致的不可见文本层；`catia_inspect_drawing`
  在图纸声明 TEXT LAYER 时跳过 OCR；新增 `compactSpans`。
- `rebuild_from_schedule.py`：数据准备侧从排表重建实体并与参考体积比较，用于判断表达是否充分。
- `verify_case.py` / `material_diff.py` / `reexport_reference.py` / `repro_build.py`：独立验收、材料差定位、
  CATIA 重导出与失败复现工具。
- `verify_case.py` 的比较改用多级 OCCT 布尔（含 fuzzy）并取不超过输入体积的最大重叠；参考 STEP 的
  游离草图边不再污染 bbox（用实体 bbox）。

## 5. 失败记录（保留，不覆盖）

- FTC-08 至少 4 次隔离运行（`ebab164a`、`13310d1d`、`6ccb609a`、`e50e11f8`）：前三次因需求过大/模型输出循环失败，
  第四次建出模型但 editability 失败，收尾时中断。
- FTC-10 两次隔离：第一次 editability 失败（HS3 无效孔深），第二次构建通过但参考差 0.117%。
- CTC-01 一次隔离：模型缺层，参考差 −91k mm³。
- 此前记录的 AM 失败（auth、runtime invalidation、registry 膨胀）仍有效。

## 6. 证据索引

| 内容 | 路径 |
| --- | --- |
| FTC-09 两次验收 | `runs/nist-pdf-benchmark/20261030/FTC-09/{run-isolated-1,run-ui-2}/acceptance.json` |
| FTC-09 结果汇总 | `runs/nist-pdf-benchmark/20261030/FTC-09/results.json` |
| STC-09 验收 | `runs/nist-pdf-benchmark/20261030/STC-09/isolated-1/acceptance.json` |
| 派生图纸（FTC-09/FTC-08/CTC-01 等） | `work/derived-20261029/` |
| 数据准备重建检查 | `work/derived-20261029/*-sections*/rebuild.json`、`section-eval.json` |
| HTC OCR/PPTX | `work/derived-20261029/HTC-ocr.json`、`HTC-ppt.txt` |
| 浏览器卡片/预览（合成件） | `work/browser-e2e-20261029/REPORT.md` |

## 7. 收尾时运行状态

- CATIA 与 dev 栈（3001 / 5173 / 18789）保持运行；未终止其他推理进程。
- 无仍在执行的生成任务（FTC-08 重试已停止）。
- 未提交 commit。

## 8. 2026-10-30 二次收尾：生成器修复与全案例排表筛选

用户要求先收尾、并取消“打通 17 例”的无人值守目标；本节记录收尾前完成的工作，
未新增任何 NIST 验收运行（FTC-08 isolated-5 在 `compile_build_plan` 阶段被停止）。

### 8.1 生成器与 schema 修复（详见踩坑 124–129）

- MCP 输入 schema 与编译器能力不一致：命名轮廓引用 `"P1"` 和紧凑数组
  `["circle",x,y,r]` 在 `catia_validate_requirements` 就被拒（踩坑 124）。
  已修 `feature_schema.py`，并加回归测试。
- 截面堆栈生成器反向边/弧行无终点导致重建出 94 万 mm² 的假面、开放 stray wire
  被当孔、rebuid 布尔 union 静默丢体积（踩坑 125–127），均已修复。
- 回归：`test_feature_model + test_feature_executor + test_feature_workflow`
  78 passed；加跑 `test_feature_model + test_drawing_requirements + test_server_runtime`
  共 103 passed。未重跑全量测试。

### 8.2 全案例排表筛选（数据准备侧，`rebuild_from_schedule.py`，非验收）

用修好的生成器对 11 个有 STEP 参考的案例生成截面堆栈并重建，误差对照冻结容差：

| 案例 | blend-samples 1 | blend-samples 3 | 冻结容差 | 结论 |
| --- | --- | --- | --- | --- |
| FTC-08 | +56.1（0.011%） | — | 100.7 | **可用**（已用于 isolated-5，运行被停止） |
| FTC-09 | −1.3 | — | 10 | 可用（已验收） |
| FTC-11 | +59.5 | — | 25 | 排表不达标，但该案例已用原始 PDF 验收，不改 |
| CTC-01 | −4,625（0.032%） | −3,291（0.022%） | 7,321 | **可用，未跑生成** |
| CTC-04 | −21,822 | **−95.1** | 175.2 | 数据准备可用（s3 有 999 页，未跑生成） |
| FTC-06 | +6,052 | −3,086 | 33.2 | 不达标（环面/球面圆角） |
| FTC-07 | +18,873 | +17,524 | 17.3 | 不达标（大量圆角+20 个 BSPLINE 面） |
| FTC-10 | −1,250 | +168.5 | 10 | 不达标（钻尖锥面/孔口圆角，见踩坑 128） |
| CTC-02 | −1,548,255 | −1,897,358 | 471 | 不达标（多轴侧向特征） |
| CTC-03 | −1,635 | −1,928 | 10 | 不达标 |
| CTC-05 | −43,328 | +11,528 | 126.9 | 不达标 |

原始排表与重建结果在 `work/derived-20261030/<case>-s1|s3/`。

### 8.3 FTC-08 isolated-5

- 输入 `work/derived-20261029/FTC-08-sections4/FTC-08.pdf`（16 pad + 30 cut，截至本轮重建 +56.1 mm³）。
- 运行到 `catia_compile_build_plan`（13 次工具调用，validate 第二次返回 ready）时被停止，
  **没有交付、没有验收**，不计入任何通过数。
- 停止前发现并修复了 124；该会话的 MCP 进程未加载修复，后续重跑必须用新会话。

### 8.4 收尾结论

- 完成验收数不变：**2/17**（FTC-11、FTC-09），STC-09 1/2。
- 数据准备筛选新增两个可跑候选：**CTC-01（s1/s3）**、**CTC-04（s3，但 999 页不现实）**。
- 其余 12 例受圆角/倒角/钻尖锥面/多轴侧向特征阻塞；需要“锐化实体 + 逐边 FILLET/CHAMFER
  标注 + 钻尖图元”，而不是继续调排表参数。

