# CATIA Agent 下一位 AI 开发交接

更新时间：2026-10-28。工作区：`F:/CatiaAgent`；Windows / PowerShell。

**当前结论：PDF → Agent 读图 → 原生可编辑 CATPart 的核心链路已通过一个合成组合件的真实隔离 Agent 验收；浏览器完整链路与 NIST 全量目标尚未完成。** 用户要求先收尾，再编写本交接；本次只整理文档，没有恢复开发。原开发目标处于 paused；下一位 AI 收到继续开发指令后再推进。

本文是当前状态入口。旧《CATIA_Agent_text-to-cad集成开发_交接说明.md》前八节属于早期历史，其中“未接入、未测试、OCR 测试失败、没有新 CATPart”等说法已经过期。不要据此重做已完成工作。实施日志按时间追加，冲突时以最新记录、当前源码和实际证据为准。

## 0.6 2026-10-30 晚间收尾（用户取消“打通 17 例”目标）

- **NIST 仍 2/17**（FTC-11、FTC-09），STC-09 1/2。FTC-08 isolated-5 在
  `compile_build_plan` 阶段被停止，没有交付，不计通过。
- **修了 4 个基础设施缺陷**（踩坑 124–127）：
  1. MCP 输入 schema 不接受命名轮廓 `"P1"` / 紧凑数组 → validate 直接拒（`feature_schema.py` 已修 + 回归测试）；
  2. 截面堆栈生成器的边方向/弧终点处理错误，重建出 94 万 mm² 假面（`oriented_edges`、`A` 行显式终点、0.2 mm 折线采样）；
  3. 相切截面的开放 stray wire 被当成孔，整层被跳过（`wire_is_closed` 过滤）；
  4. `rebuild_from_schedule.py` 的链式布尔 union 静默丢体积 → 改 `Compound` 求和。
- **全案例排表筛选**（`runs/nist-pdf-benchmark/20261030/report.md` 第 8 节）：
  数据准备侧可达标的新候选：**CTC-01**（−0.032%，容差 0.05%）、
  **CTC-04**（s3 −95 mm³，容差 175，但 999 页不现实）。
  FTC-06/07/10、CTC-02/03/05 仍被圆角/钻尖/多轴特征阻塞；FTC-10 的 10 mm³
  冻结容差与截面堆能力不匹配（踩坑 128），不能靠放宽容差通过。
- OCCT `BRepAlgoAPI_Defeaturing` 对 NIST 参考不可用（无效实体，踩坑 129）；
  下一步应做“锐化实体 + 逐边 FILLET/CHAMFER + 钻尖图元”，而不是继续调采样数。
- 未提交 commit；改动文件：`catia_mcp/drawing/feature_schema.py`、
  `tests/test_feature_model.py`、`datasets/nist-pdf-benchmark/derived/generate_layer_schedule.py`、
  `derived/generate_section_schedule.py`、`tools/rebuild_from_schedule.py`。

## 0.5 2026-10-30 更新（最新）

- **NIST 2/17 完成**：FTC-11（沿用 10-28 两次运行）与 **FTC-09**（本批次两次最终运行均 PASS：
  隔离生成 + 真实 UI 上传；均对**原始 STEP** 独立重开与材料比较 PASS，缺失 3.55 / 多余 1.75 mm³，容差 10）。
  **STC-09 完成 1/2**（隔离 PASS，UI 未跑）。证据：`runs/nist-pdf-benchmark/20261030/`。
- **生成模型已换为 Claude Opus 5.5**（`modelbest-anthropic`，Anthropic 协议，timeout 900 s，
  maxOutputTokens 32768，thinking medium）。DeepSeek flash 无法稳定产出大型结构化需求。
  此前用 flash 的 FTC-11 验收保持不变（当时模型记录在案）。
- **需求语言**：草图坐标可写数值；紧凑数组轮廓；顶层 `profiles` 命名轮廓表（feature 用 `"P1"` 引用）。
- **派生图纸**：新增 `generate_section_schedule.py`（截面堆栈：L 行 pad + C 行切除 + S 侧向特征）、
  文本层叠加、TEXT LAYER 免 OCR、`compactSpans`；`rebuild_from_schedule.py` 用于验证排表表达是否充分。
- **关键基础设施修复**：参考 STEP 的游离边污染 bbox（改用实体 bbox）；OCCT 布尔对公差壳返回
  0/sliver（多级 fuzzy 取最大合理重叠）；Gateway 工具结果上限 20k→200k；runner 报告查找与
  `generated_verified` 判定；PartNumber 冲突回退；参数改值抽样与自适应容差；证据按页合并。
- **剩余 15 例的共性阻塞**：多轴侧向特征、曲面圆角/倒角（环面/锥面/球面）未进入需求语言与排表；
  HTC 无可测量参考（V5-6R2023 打不开，PDF 无内嵌 3D）。详见
  `runs/nist-pdf-benchmark/20261030/report.md` 第 3 节。
- 新踩坑 116–123。服务保持运行；未提交 commit。

## 0. 2026-10-29 更新（接手先读）

- **组合语言新增 `fillet` / `chamfer`**：几何边选择器（直线=方向+线上一点；
  圆边=半径+圆心+轴向），必须唯一命中；同一条圆边的多个重合引用会逐个尝试。
  真实 CATIA 验收通过，隔离 Agent 能仅凭图纸自主使用圆角+倒角，交付与参考 STEP
  零材料差。证据 `work/isolated-generation/c1ac674b-0d1a-4324-be04-367ee9749e50/`。
- **浏览器全链路通过（合成图纸 COMPOSITION-01）**：上传→建模→交付→交付卡片→3D 预览。
  修了两个阻断：`ui/dist` 过期、结构化校验明细触发 React #31；以及 General 工作区
  预览文件 403。截图与结论 `work/browser-e2e-20261029/REPORT.md`。
- **NIST 仍是 1/17**。派生图纸工具 `datasets/nist-pdf-benchmark/derived/generate_derived.py`
  已能生成视图+标注+坐标表，但 FTC-09 试验稿不完整，**没有用于验收**；
  其余 16 例仍缺充分输入。不要把它当已完成能力。
- 新踩坑 109–115：边几何读取（不能用 GetCOG）、notes 占视图高度、STEP soundness、
  运行中改插件导致 result_unknown、registry 膨胀、前端卡片崩溃、General 预览路径。
- 操作规则：**生成回合进行中不要安装/修改插件、技能或重启 dev 栈**；长任务前先备份并
  清空 `PILOT_HOME/runs/registry.json` 的 `events`（本轮它涨到 22 MB 并诱发
  `result_unknown`）。隔离 runner 需要 `PILOT_HOME` 和 `--import tsx`。
- 本批次报告：`runs/nist-pdf-benchmark/20261029/report.md`。

## 1. 最终目标与不能改变的验收口径

用户上传 PDF 工程图，由 AgentCPM 理解图纸、规划并调用 CATIA，从零生成完整、原生、参数可编辑的 CATPart，同时提供追溯和预览。

- 不按图号、文件名、哈希查找已有答案；不导入 STEP 再另存 CATPart 冒充原生建模。
- 参考 CAD 仅用于准备图纸和独立评价；不能向生成 Agent 暴露参考文件、预期尺寸、隐藏比较结果或答案。
- CATIA Update 成功、特征树有对象、保存成功均不足以验收。需要独立重开、几何读回/精确比较、逐参数副本修改验证。
- 生成端基于自身需求计算的预期几何，只证明执行符合计划；还需独立判断计划是否符合输入图纸。隐藏参考比较是额外证据，不能拿来回填生成答案。
- 原始 PDF 确实信息不足时，可以准备保留完整几何、尺寸充分的派生 PDF。原始与派生实验分别统计，不能删掉难特征凑通过。
- NIST 共 17 个零件：STC-06～10、HTC、FTC-06～11、CTC-01～05。目标实验量为 17 次原始基线 + 34 次原始最终复测；若有 d 个派生案例，再加 2d 次完整验收。拒绝/不支持不算完成。

先读：`CLAUDE.md`、`项目需求.md`、`CATIA_Agent_NIST全量实验与能力扩展开发方案.md`。修改或操作 CATIA 前，还需读服务端 `.claude/skills/catia-v5-mcp-maintainer/SKILL.md` 和 `.claude/skills/catia-v5-production-operator/SKILL.md` 及其相关引用文件，尤其 topology、verification、failure recovery、result contract。遇到已知问题先查 `踩坑报告.md`，最近条目为 107、108。

## 2. 已完成与尚未完成

| 范围 | 当前状态及边界 |
| --- | --- |
| 辅助 CAD | cadgen 制图、真实 OCR 读回、OCCT 检查/精确材料比较、三角网格预览；通过独立 Python 子进程调用，未将 OCP 塞入长期 COM 进程 |
| 组合建模 | Pad/Pocket/Shaft/Groove；矩形、圆、多边形、直线/圆弧混合轮廓；xy/yz/zx、偏移平面、参数表达式和证据引用已接入主链路 |
| 参数与验收 | 原生约束/公式、独立重开、STEP 精确比较、逐参数修改工作副本、步骤追溯和交付报告已实现；多组真实 CATIA 合成案例通过 |
| 三维预览 | 新旧建模流程生成 `.preview.json`，聊天卡片接入 WebGL；独立浏览器烟雾测试通过，实际上传到聊天卡片的完整链路未通过 |
| 隔离生成 | 新工作区仅放 PDF；生成侧只有 inspect/crop/validate/compile/build 五工具；路径、链接逃逸限制和内嵌裁图已验证；不是操作系统沙箱 |
| 真实 Agent | 一个合成组合件通过 Gateway CLI：自行读图、规划、原生建模、8 参数修改验收、隐藏参考比较通过。不是浏览器验收，也不是 NIST 案例 |
| 圆角/倒角 | 原生 API/公式传播探针通过；既有倒角工具枚举错误已修。尚未接入组合特征语言，探针尚缺独立重开与精确材料比较 |
| NIST | 历史 FTC-11 两次原始 PDF 完整成功，即 1/17；其余 16 例未完成。不能把本轮合成探针计入 NIST |

没有提交 commit。`AgentCPM` 与 `catia-v5-mcp-server` 均存在大量既有未提交/未跟踪文件，不能将整个 dirty 状态都归为本次开发。禁止用 reset/checkout/clean 清掉用户成果；原始 CATPart、备份和实验记录不能覆盖。

## 3. 最关键的证据与交付件

### 3.1 真实隔离 Agent 成功案例

根目录：`work/isolated-generation/e21740ad-5878-4fe1-b247-9fb9e5b2e7d0/`。

| 相对该根目录的路径 | 用途 |
| --- | --- |
| `generation/drawing.pdf` | 生成侧唯一初始输入 |
| `generation/deliveries/part.CATPart` | 正式原生交付，97037 字节 |
| `generation/deliveries/part.build.json` | 构建、独立几何、参数修改及追溯报告 |
| `generation/deliveries/part.step`、`part.preview.json` | 生成件导出和预览，预览 528 个三角形 |
| `host/manifest.json` | `generated_verified`，终态 `turn_completed` / `finishReason=completed` |
| `host/events.jsonl` | 完整工具调用事件；共 10 次，全部在五工具范围内 |
| `host/hidden-comparison.json` | 隐藏参考比较 PASS；包围盒、缺失材料、额外材料误差均为 0 |

运行时间为北京时间 2026-10-28 17:25:45–17:32:48。图纸源为 `work/composition-drawing/bracket.pdf`，SHA256 为 `6deb869e54529a4e06f435a0862ed02decab4248c4993f8094da687041ddaeb2`。

这是板 + 圆凸台 + 同轴通孔的组合件，8 个参数全部通过副本改值验证。实测体积 9713.09733552923 mm³，bbox 为 [0,0,0]–[40,30,12] mm。这些参考信息仅供开发者审计，不应被写入新生成会话提示词。隐藏比较在构建完成后执行，文件保留于 host，没有送回生成 Agent。

### 3.2 其他证据索引

以下路径均相对于工作区；这是证据导航，不代表所有实验均通过完整最终验收。

| 案例 | 证据路径 / 结论 |
| --- | --- |
| Shaft + Groove | `work/feature-tools-1793173952612639900/`；3 参数改值与精确比较通过 |
| 凹多边形 + 三角盲槽 | `work/feature-tools-1793176619132503500/`；体积 7050 mm³，3 参数修改通过 |
| 长圆槽 | `work/feature-tools-1793177307812973900/`；4 参数修改通过 |
| 顺时针大/小弧 | `work/feature-tools-1793177550761878600/`、`work/feature-tools-1793178024184948900/` |
| 逆时针大/小弧 | `work/feature-tools-1793178077381992100/`、`work/feature-tools-1793178145864300300/` |
| 真实 stdio 隔离边界 | `work/generation-scope-1793178751054182100/report.json`；6 检查通过，未建模 |
| fillet API 探针 | `work/edge-finish-fillet-1793180037967340000/`；公式修改产生正确体积，尚非完整验收 |
| chamfer 公开工具探针 | `work/edge-finish-chamfer-1793180173868876900/`；2 mm×45° 体积 9520，改 2.2 mm 后 9503.2 mm³ |
| 浏览器 DP-01 中断 | `work/text-to-cad-ui-20261028/`；会话 `web-s_48c2cd5b-b7cc-4723-bff3-40ee36f9d2ed`，Gateway 崩溃，不算通过 |
| 中断件事后检查 | `work/interrupted-ui-verification.json`；13 项通过，但不能补算浏览器全链路成功 |

更多历史证据见 `catia-v5-mcp-server/docs/text-to-cad-integration.md`，尤其最后一节。

## 4. 源码入口：继续工作应在哪里改

下表是阅读入口，不是精确的本轮 git diff 清单。

| 模块 | 主要路径 |
| --- | --- |
| 特征需求、编译、公开 schema | `catia-v5-mcp-server/catia_mcp/drawing/feature_model.py`、`feature_schema.py`；路由在同目录 `requirements.py`、`plan.py`、`tools.py` |
| CATIA 执行与流程验收 | 同目录 `feature_executor.py`、`feature_workflow.py`；旧流程为 `builder.py`、`verify.py` |
| 独立预期几何 | `catia-v5-mcp-server/catia_mcp/feature_geometry.py` |
| 独立重开检查 | `catia-v5-mcp-server/scripts/independent_verify_part.py` |
| 制图/比较/预览辅助进程 | `catia-v5-mcp-server/catia_mcp/cad_support.py`、`cad_support_client.py`；`drawing/preview.py` |
| OCR | `catia-v5-mcp-server/scripts/ocr_image.py`：896 像素分块、128 重叠、坐标还原 |
| 生成侧路径/工具限制 | `catia-v5-mcp-server/catia_mcp/generation_scope.py`、`server.py` |
| 倒角修复 | `catia-v5-mcp-server/catia_mcp/tools/part_design.py`；`tests/test_chamfer_mode.py` |
| 宿主图片路径限制 | `AgentCPM/src/mcp/runtime/PluginToToolBridge.ts`；`tests/mcp/mcp-runtime-port.spec.ts` |
| 前端交付卡/预览 | `AgentCPM/ui/src/composition/modules/catia-edit.tsx`、`catia-preview.tsx` |
| Agent 建模协议 | `AgentCPM/products/catia-agent/plugins/catia-edit/skills/catia-pdf-model/SKILL.md` |
| 隔离实验准备/运行 | `AgentCPM/products/catia-agent/acceptance/prepare-generation-run.mjs`、`run-prepared-generation.mjs` |
| 实验边界说明 | 同目录 `pdf-generation-isolation.md` |
| 现场探针 | `catia-v5-mcp-server/tests/acceptance/run_feature_tools.py`、`run_edge_finish_probe.py`、`generation_scope_e2e.py` |

组合协议为 `catia-feature-requirements/1` 和 `catia-feature-plan/1`。先阅读当前 schema/测试再扩展，不从旧文档猜字段。`wire` 以起点和 line/arc 有序段定义，弧包含端点、半径、clockwise、largeArc；保留真实圆弧，不能折线近似实际设计曲线。

## 5. 环境与运行状态

- CATIA/主测试 Python：`F:/CatiaAgent/catia-v5-mcp-server/.venv/Scripts/python.exe`。
- cadgen/OCP/OCR 辅助 Python：`F:/CatiaAgent/work/text-to-cad-env/Scripts/python.exe`。运行配置使用 `CATIA_CAD_SUPPORT_PYTHON`、`CATIA_OCR_PYTHON`；恢复时核对当前配置，避免重复安装或混环境。
- Node 需要 `>=22.13 <23`。已用路径：`F:/CodingAgent/claude-code/work/CatiaAgent/20261021-node22/node/node-v22.23.3-win-x64/node.exe`。不要用系统 Node 24。
- `PILOT_HOME=F:/CatiaAgent/work/ui-home-20261024`；配置 `AgentCPM/products/pilotdeck-staffdeck-sop/profiles/native.yaml`；启动入口 `start-pilotdeck.cmd`。
- 暂停时端口：UI 3001、Vite 5173、Gateway 18789。PID 观察值：Gateway 69412、监督进程 97044、UI 47848、Vite 92988、CATIA 68612。**这是历史观察值，必须重新核对命令行和活动会话，不能按旧 PID 杀进程。**
- 暂停时无仍在执行的生成、建模、独立验收任务；保留上述常驻服务。此文档整理没有重新检查其健康或重启服务。
- Gateway 已在确认会话空闲后加载宿主图片路径限制。最新 PDF 技能已安装到 PILOT_HOME 的插件及独立技能目录并 reloadExtensions；备份 `work/catia-skill-backup-20261028-1729/`。
- 最新倒角源码在新的 MCP 进程中加载；暂停前未为此额外重启常驻服务。
- CATIA COM 继续使用既有单 STA、机器级锁。只关闭本次创建/确认归属的文档；不要终止其他 Python 推理进程。

### 可复跑的收尾测试

最后收尾主环境测试为 **131 passed**，Node 为 **6 passed**。此前同轮 OCCT 几何组 **15 passed**，完整 TypeScript 检查通过。它们是已记录结果，不表示本文整理时又全量重跑。

```powershell
Set-Location 'F:/CatiaAgent/catia-v5-mcp-server'
& '.venv/Scripts/python.exe' -m pytest tests/test_feature_model.py tests/test_feature_executor.py tests/test_feature_workflow.py tests/test_drawing_requirements.py tests/test_drawing_verifier.py tests/test_generation_scope.py tests/test_server_runtime.py tests/test_chamfer_mode.py tests/test_topology_search.py -q
& 'F:/CatiaAgent/work/text-to-cad-env/Scripts/python.exe' -m pytest tests/test_feature_geometry.py -q

Set-Location 'F:/CatiaAgent/AgentCPM'
$catiaNode = 'F:/CodingAgent/claude-code/work/CatiaAgent/20261021-node22/node/node-v22.23.3-win-x64/node.exe'
& $catiaNode --import tsx --test tests/mcp/mcp-runtime-port.spec.ts products/catia-agent/acceptance/prepare-generation-run.test.mjs
```

改动后选适用回归；纯离线通过不能替代真实 CATIA、独立验证或浏览器验收。大型 OCR/几何运行前查看内存余量，曾发生系统提交内存耗尽和 Gateway 崩溃；不要把资源失败静默算成功。

## 6. 如何继续真实隔离实验

先读 `AgentCPM/products/catia-agent/acceptance/pdf-generation-isolation.md`。确认服务、合法认证配置、已安装技能和空闲状态后，以 Node 22 在 AgentCPM 目录运行：

```powershell
& $catiaNode products/catia-agent/acceptance/prepare-generation-run.mjs --pdf 'F:/path/input.pdf' --runs-dir 'F:/CatiaAgent/work/isolated-generation'
# 从准备脚本输出取本次新 UUID 下的 host 路径；不要复用旧实验目录。
& $catiaNode products/catia-agent/acceptance/run-prepared-generation.mjs 'F:/CatiaAgent/work/isolated-generation/<新UUID>/host'
```

准备脚本仅创建实验，`prepared_not_executed` 不算运行。运行脚本通过本地 UI API 登记已有工作区，再向 Gateway 提交一次，事件写入 host。此前三个前置失败目录保留，不覆盖成成功。

关键约束与已踩坑：

- SDK `skills: []` 不合法；当前选择已安装的 `catia-edit:catia-pdf-model`，提示词取当前通用技能。不能直接选择尚未注册的新 SDK 插件技能。
- 新目录必须先登记项目。当前脚本固定访问 127.0.0.1:3001；此前环境认证关闭，其他环境需合法认证，不能绕过。
- 五工具白名单由宿主与专用 MCP 同时限制；`CATIA_GENERATION_ROOT` 要求绝对路径且拒绝目录外/链接逃逸。不得增加 shell、read_file、其他 MCP 或参考 verify/import。
- 参考答案、主机记录、隐藏比较置于 generation 的兄弟 host 目录；不能复制进生成目录。不同实验保持独立事件与报告。
- 生成结束后再做独立隐藏比较，并检查事件终态、交付报告和材料差异。不能只读 manifest 的一个状态字段宣称成功。现有运行脚本仍有加强终态判定的空间：应核对 `finishReason=completed` 等，当前成功案例已人工核对。
- 截图/图面通过 crop 内嵌 PNG 供模型读取。模型曾使用相对输出路径被拒绝，随后改为允许目录内绝对路径完成；不要为消除拒绝而放宽边界。

## 7. 下一步建议顺序与完成条件

1. **恢复现场与基线。** 读本文和契约，检查两仓库状态及现有服务/活动会话，核对成功证据；按变更范围运行必要测试。先确认已有成果，不再从早期 OCR 失败或“原型未接入”重新开始。
2. **按 NIST 实际缺口补共性特征。** 审查逐例图纸/参考准备记录，建立尺寸充分性与所缺特征清单。优先延续 fillet/chamfer 探针，把 schema、编译、执行、预期几何、独立重开、参数修改、Agent 协议和失败路径贯通；其他所需特征同样处理。边选择必须基于当前实体重新解析并唯一匹配，不能依赖跨操作失效的边编号。
3. **完成充分图纸准备与独立审查。** 不把只有外形尺寸的派生 PDF 当完整图纸；逐例核对源几何、尺寸、视图、provenance。部分旧 NIST 参考文件版本不可直接打开，需核实已有替代来源及等价证据；不能默认为所有参考都可用。
4. **补浏览器完整验收。** 实际上传 PDF → Agent 建模 → 最终交付 → 聊天卡片通过项目文件服务加载真实预览；保存事件和截图。独立测试页或事后重开不能代替这条链路。
5. **完成 NIST 全量实验与复测。** 按第 1 节口径逐例记录原始/派生输入、运行 ID、特征完整性、几何和参数验收、预览及失败原因；不能只统计工具调用结束。
6. **更新支持声明和最终报告。** 所有要求均完成后才把整体任务标为完成。记录已证实与未证实的边界，保留失败历史。

边缘 API 的已确认事实：本机 `PartTypeLib.tlb` 中 `catTwoLengthChamfer=0`、`catLengthAngleChamfer=1`；旧工具把 mode=0 和 angle=45 搭配使用会误成第二长度 45 mm，已改为 mode=1。记录在 `work/part-typelib-edge-api.json`、`踩坑报告.md` 第 108 条。探针不等于功能已集成；圆角也不能一律假定减材料，需要按真实几何验证。

### 尚待用户选择的环境事项

先前 ego-browser TaskSpace 1 已消失；根据当时读取的技能要求，已询问是否允许重建，未得到选择。MinerU 本地解析不可用时也已询问改用现有本地解析、配置本地引擎或远程解析，未得到选择。没有擅自新建替代空间或上传 NIST 文档。

接手时检查当前技能和用户后续答复；不要将沉默当成授权，也不要仅为这两项停止所有工作，可以继续不依赖它们的代码、离线回归和本地 CATIA 特征开发。需要提问时说明具体阻塞及来源，避免重复索取已经给出的授权。

## 8. 给下一位 AI 的极简启动提示词

> 继续开发 F:/CatiaAgent。先读 CATIA_Agent_下一位AI开发交接_2026-10-28.md，按其中最新状态和续开发顺序推进。保留现有改动，严格隔离生成与参考答案，完成剩余功能、浏览器链路和 NIST 验收；不要重做已完成项或把部分通过报成全部完成。

