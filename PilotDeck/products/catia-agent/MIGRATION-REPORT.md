# PilotDeck CATIA Agent 迁移报告

- 执行日期：2026-11-05
- 目标基座：`F:\CatiaAgent\PilotDeck`
- 目标提交：`32a291f65417af9358a54e13072f5a8a1752ad18`（`main`，浅克隆；迁移期间未更新到其他版本）
- 旧基线：`F:\CatiaAgent\AgentCPM` HEAD `d5bd96a714fd1feb914aa9ff8a62d65f69eb41e8` + 未提交工作区
- MCP server：`F:\CatiaAgent\catia-v5-mcp-server` HEAD `8a87e7c7c519cfdd0aea5e50b04651db66340e5e` + 既有未提交修改（本次仅加 2 处路径规范化 + 1 个测试）
- 迁移工作区：`F:\CatiaAgent\work\pilotdeck-migration-20261105\`

## 1. 结论摘要

CATIA Agent 的两条主链路已在新基座上跑通，并用真实 CATIA 与独立读回复核：

1. **参数化改型**：新 PilotDeck 加载 `catia-edit` 插件 → DeepSeek 模型读取 `catia-parametric-edit` 技能 → 调用 `catia_begin_edit → compile_intent → apply_edit_plan → finalize_edit` → 交付 `delivery.CATPart`；独立只读脚本复核 `source_sha256/reopen/volume/bbox/holes/drivers/feature_count/physical_hole_probe/original_untouched/cleanup` 全部 pass。
2. **PDF 图纸建模**：同一新基座下，模型读取 `catia-pdf-model` 技能 → `catia_inspect_drawing → validate → compile → build_part`，建立 DP-01（120×80×10，4×Ø10 贯穿孔）并交付；独立复核全部 pass。

同时迁移了 CATIA 相关的可靠性语义：build 结果形状校验、失败状态 `reportedCatiaStatus` 透传、结果未知/忙碌围栏、`_meta` 操作 deadline、工具失败恢复建议、推理中断重试。新基座没有的旧子系统（RunRegistry/run events/terminal evidence/PDF 隔离/前端业务模块）**未迁移**，影响与替代见第 8 节。

## 2. 基线与差异

### 2.1 三个仓库状态

| 仓库 | 分支 / HEAD | 工作区状态 | 处理 |
|---|---|---|---|
| `PilotDeck` | `main` / `32a291f…` | 浅克隆；51 个 LFS 文件；`scripts/computer-use/icon.png` 是真实 PNG，不是 LFS 指针 | 作为新基座；`ui/public/**` LFS 图标已 `git lfs pull`；其余 LFS 文档/视频未拉取（运行不需要） |
| `AgentCPM` | `p1p2-inject-user-text` / `d5bd96a…` | 46 个已跟踪修改 + 60 个未跟踪（CATIA 产品/验收/核心补丁） | 只读基线，未修改、未清理 |
| `catia-v5-mcp-server` | `p1p2-structure-context` / `8a87e7c…` | 80 项既有修改（PDF/编辑/结构证据） | 保留用户修改；仅加路径规范化修复与测试 |

### 2.2 共同历史

新仓库是 `OpenBMB/PilotDeck` 的浅克隆，只有一个提交；旧 `AgentCPM` 没有共同可见历史（`git cat-file` 在新旧仓库互查目标 HEAD 均失败）。因此没有按 commit 对比，而是按目录、接口、行为和新基座源码语义逐项对照。旧仓库工作区与旧 HEAD 的差异已保存到 `baseline/agentcpm-*.txt`、`baseline/diff-*.txt`。

### 2.3 差异分类

完整逐项表见 `reports/capability-inventory.md`。摘要：

| 分类 | 数量/范围 | 代表 |
|---|---|---|
| 新基座已有，无需迁移 | MCP `structuredContent`、按 annotations 重放、插件/技能加载、上传与附件路径、通用工具失败恢复、Gateway 流式事件 | `src/mcp`、`src/extension`、`ui/server` |
| 直接迁移 | 插件/2 技能/2 命令、统一空回复提示、嵌套 deadline 保护 | `products/catia-agent/**`、`AgentLoop.ts` |
| 适配迁移 | CATIA 结果校验/失败状态/围栏/`_meta` deadline、推理中断重试、UI 状态透传 | `catiaFailureStatus.ts`、`McpClient.ts`、`PluginToToolBridge.ts`、`streamModel.ts` 等 |
| 暂不迁移 | RunRegistry/SQLite/终端证据/owned Gateway/PDF 隔离/旧验收脚本/CATIA 卡片 | 新基座没有对应子系统；见第 8 节 |

## 3. 能力验收表

| 能力 | 样例 | 预期 | 实际 | 证据 |
|---|---|---|---|---|
| 插件发现 | 迁移 home `plugins/catia-edit` | 发现 `catia-edit`，含 2 技能、2 命令、`catia` MCP | 通过：`skills=[catia-edit:catia-parametric-edit, catia-edit:catia-pdf-model]`，`commands=[catia-edit:pdf-model, catia-edit:verify-part]` | `logs/link-test.log` |
| MCP 连接/工具清单 | `plugin.json` 的 `catia` server | 启动 MCP server，工具 wire name `mcp__catia__*` | 通过：`status=ready`，115 个工具 | 同上 |
| MCP 工具调用（只读） | `catia_inspect_drawing` 读 DP-01 | 返回 `DRAWING_READ`、证据 manifest | 通过：26 个 span，`evidencePath=artifacts/drawing-evidence-dp01/evidence.json` | 同上、`logs/inspect-dp01.log` |
| 失败传播（单元） | 伪造 `VERIFIER_SESSION_UNKNOWN` / `MATERIAL_COMPARISON_INCONCLUSIVE` / 畸形 envelope | 失败不能被当成成功；`reportedCatiaStatus` 类型化；恢复建议禁止自动重试 | 通过 19/19 | `logs/new-tests-dist.log`、`tests/mcp/*` |
| 失败传播（真实 MCP server） | 向 `catia_build_part` 传入不完整 requirements | 真实 `INVALID_ARGUMENT` 失败不能变成成功；客户端进入围栏，恢复建议禁止重试 | 通过：`reportedCatiaStatus.code=CATIA_BUILD_UNCLASSIFIED`，`automaticRetryAllowed=false` | `artifacts/real-catia/failure-propagation.json`、`logs/real-failure-propagation-2.log` |
| CATPart 参数编辑（模型 E2E） | B31 兼容夹具，`L` 160→200 mm | 走技能完整流程，交付并独立复核 | 通过：`begin_edit` 会话 `c0d96bd785b5`；`compile_intent` planned；`apply` PASS；`finalize` completed；交付 sha256 `064dffb0…`；独立复核全 pass | `logs/e2e-model-edit.log`、`artifacts/model-edit/independent-verify.json` |
| CATPart 参数编辑（MCP 直连） | 同上，独立会话 | 同上 | 通过：`finalize` `COMPLETED`，7 项检查 pass；体积 238115.044408 mm³ | `logs/real-parametric-edit-5.log`、`artifacts/real-catia/A01-independent-verify.json` |
| PDF 图纸建模（MCP 直连） | DP-01（120×80×10，4×Ø10） | `BUILD_COMPLETED`、PASS、editability pass | 通过：14 项检查，体积 92858.407346 mm³，bbox 120×80×10，4 孔 (±40,±20) Ø10，改 L 120→130 体积 100858.4073 | `artifacts/real-catia/DP-01-build-result.json`、`DP-01-independent-verify.json` |
| PDF 图纸建模（模型 E2E） | 同上，走 `catia-pdf-model` 技能 | 模型自行读图、引用 span、build | 通过：交付 sha256 `35cdde61…`，93919 B；14 项检查；独立复核全 pass | `logs/e2e-model-pdf.log`、`artifacts/model-pdf/independent-verify.json` |
| 文件上传 → 模型 → 产物 | 通过 `UploadStore` 上传 DP-01（upload `e9b41be3…`），模型从附件路径建模 | 附件登记、模型定位、建模、交付、独立复核 | 通过：交付 sha256 `dbea8e2d…`，93796 B；14 项检查；独立复核全 pass | `logs/e2e-upload-pdf.log`、`artifacts/upload-pdf/independent-verify.json` |
| Gateway 模型回合 | “调用 catia_diagnose 报告版本/状态” | 新基座加载 DeepSeek + 插件 + MCP，工具成功、回合结束 | 通过：`tool_call_finished mcp__catia__catia_diagnose ok=true`；`turn_completed finishReason=completed` | `logs/e2e-gateway-2.log` |
| 产物展示 | 模型总结 + 工具结果 | 交付路径、sha256、verdict/checks 可见 | 通过：模型总结含路径/sha256/check 列表；工具结果 JSON 含 `delivery` | 同上、`e2e-model-edit.log` 结尾 |
| UI 状态 | 重试状态、工具结果帧 | `reasoning_restart` 文案、CATIA 失败状态字段 | 通过单元/组件测试 147/147；浏览器 UI 未做自动化点击验证 | `logs/ui-vitest-targeted.log` |

## 4. 修改文件

### 4.1 新基座 `PilotDeck`（未提交）

| 文件 | 改动 |
|---|---|
| `products/catia-agent/**`（新增） | 插件、2 技能、2 命令、新基座配置模板、安装脚本、迁移启动脚本、README |
| `src/tool/protocol/catiaFailureStatus.ts`（新增） | 失败状态解析、build 成功形状校验、text/structuredContent 一致性、`catia`/`catia_*` 识别 |
| `src/mcp/client/McpClient.ts` | `operationDeadlineMs`/`_meta`；CATIA build 结果校验；build 进行中/结果未知围栏；`mcp_session_unknown` |
| `src/mcp/runtime/McpRuntime.ts` | `stop()` 改为 `allSettled`：围栏客户端拒绝关闭时不阻断其他客户端和网关清理 |
| `src/mcp/runtime/PluginToToolBridge.ts` | CATIA build 成功/失败校验；`reportedCatiaStatus` 细节；失败不可冒充成功 |
| `src/tool/execution/errorRecovery.ts` | `CATIA_STATUS` 恢复建议，禁止自动重试 |
| `src/tool/execution/ToolRuntime.ts` | 嵌套工具不能覆盖 `operationDeadlineMs` |
| `src/tool/protocol/types.ts` | `PilotDeckToolRuntimeContext.operationDeadlineMs` |
| `src/gateway/client/InProcessGateway.ts` | 错误工具结果投影 `reportedCatiaStatus` |
| `src/agent/loop/AgentLoop.ts` | 7 处空回复/输出上限提示统一为“继续工作或使用工具” |
| `src/model/streaming/streamModel.ts` | 推理缓冲、`reasoning_restart` 整请求重试、完成哨兵后 socket 结束不重放 |
| `src/router/protocol/events.ts` | 重试原因新增 `reasoning_restart` |
| `ui/server/pilotdeck-bridge.js` | CATIA 失败状态透传、`reasoning_restart` 文案 |
| `ui/src/components/chat-v2/MessagesPaneV2.tsx`、`ui/src/i18n/locales/{en,zh-CN}/chat.json` | 推理重启状态展示 |
| `tests/tool/protocol/catia-failure-status.spec.ts`、`tests/mcp/catia-build-failure.spec.ts`、`tests/mcp/client/catia-session-fence.spec.ts`（新增） | 19 项回归测试 |

### 4.2 MCP server

| 文件 | 改动 |
|---|---|
| `catia_mcp/editing/coordinator.py:1547` | `deliveryPath` 在冲突检查/SaveAs 前 `os.path.normpath` |
| `catia_mcp/editing/session.py:94` | `create_working_copy` 的 `work_dir` 先 `os.path.abspath` |
| `tests/test_editing_working_copy.py:95` | 新增正斜杠工作目录回归测试 |

### 4.3 旧工作区

`AgentCPM` 未修改。`踩坑报告.md` 新增第 198 条（正斜杠 SaveAs 失败），按四段式记录现象/根因/解决/验证。

## 5. 配置与启动

### 5.1 环境

- Node：`F:\CodingAgent\claude-code\work\CatiaAgent\20261021-node22\node\node-v22.23.3-win-x64`（v22.23.3）
- Python：`F:\CatiaAgent\catia-v5-mcp-server\.venv\Scripts\python.exe`（3.12.2）
- pnpm：10.32.1（corepack），store 在迁移工作区
- CATIA：V5-6R2021 `B31 / Release 31 / SP7`（中文界面）

### 5.2 安装

```powershell
$env:PILOT_HOME = 'F:\CatiaAgent\work\pilotdeck-migration-20261105\home'
powershell -ExecutionPolicy Bypass -File F:\CatiaAgent\PilotDeck\products\catia-agent\scripts\install-plugin.ps1
```

### 5.3 启动

```cmd
set MODELBEST_API_KEY=<从既有安全位置注入>
F:\CatiaAgent\PilotDeck\products\catia-agent\scripts\start-pilotdeck-migration.cmd
```

脚本固定：`PILOT_HOME`、`PILOTDECK_CONFIG_PATH`、`CATIA_MCP_PYTHON`、`CATIA_MCP_WORK_DIR`、`TEMP/TMP/npm cache` 均在 `F:\CatiaAgent\work\pilotdeck-migration-20261105\` 下；端口 3001/5173/18789；不读取/修改默认 `%USERPROFILE%\.pilotdeck`。需要从 WMI 拉起的场景见脚本头部注释。

配置模板：`PilotDeck/products/catia-agent/config/pilotdeck.yaml`，模型固定 `modelbest/deepseek-v4.1-flash-mb`，密钥只从 `MODELBEST_API_KEY` 环境变量读取。

## 6. 测试与实机证据

### 6.1 构建与离线测试

| 项目 | 结果 | 证据 |
|---|---|---|
| `npm run build`（PilotDeck） | 通过（tsc + builtin 插件复制 + web build metadata） | `logs/pilotdeck-build-2.log` |
| `npm run build`（UI） | 通过（vite 6829 modules，23.95 s） | `logs/ui-build.log` |
| 新增 CATIA 单测 | 19/19 pass | `logs/new-tests-dist.log` |
| 相关 Node 套件（mcp/tool/streaming/agent-loop/gateway） | 484 pass / 13 fail（均为基线 Windows 路径分隔符、symlink 权限、PATH bash、FunASR、图片生成用例；重跑两次失败集合一致，未因本次改动新增） | `logs/pilotdeck-tests-targeted-2.log` |
| 全量 Node 套件（`dist/tests/**/*.test.js` + `*.spec.js`） | 1080 pass / 16 fail / 7 cancelled / 1 skipped / 1104 total；失败为基线 Windows 路径分隔符（`.pilotdeck/tool-results`）、symlink 权限、PATH bash、FunASR、networkFetch 计时器、图片生成用例；未见与本次改动相关的新失败 | `logs/pilotdeck-tests-full.log` |
| UI 相关测试 | 147/147 pass（bridge 两套、uploads、MessagesPane） | `logs/ui-vitest-targeted.log` |
| MCP server pytest | 773 pass / 1 fail / 27 skip / 1 xfail；失败为既有 `test_sdk_handler_preserves_structured_failure`（mcp 1.30 在非请求上下文读 `request_context.meta`，生产路径不受影响）；`tests/test_cad_support_iso.py` 因 venv 无 `build123d` 收集失败 | `logs/mcp-pytest-3.log` |
| Ruff | 全量 622 条既有问题（基线未清理）；本次新增 Python 行未新增告警 | `logs/mcp-ruff-2.log` |

### 6.2 真实 CATIA（隔离输出）

环境：本次开始时没有 CATIA 进程；MCP server `Dispatch` 新建实例，日志 `CATIA launch environment repaired`。所有输出在 `F:\CatiaAgent\work\pilotdeck-migration-20261105\artifacts\`，未触碰用户 CATPart/数据集/旧运行记录。测试结束后只关闭了本次拉起的 CATIA 实例。

夹具说明：`catia-v5-mcp-server/tests/fixtures/FixtureA-mounting-plate.CATPart` 在本机 B31 上 `Documents.Open` 直接 E_FAIL（不同 Release 的夹具，见踩坑报告第 83 条）；改型测试使用可打开的 `F:\CatiaAgent\work\fixtures-b31\FixtureA-mounting-plate.CATPart` 副本，未修改原夹具。

| 用例 | 结果 | 独立读回 |
|---|---|---|
| DP-01 PDF→Part（MCP 直连） | `BUILD_COMPLETED`，14 checks PASS，editability L 120→130，体积 92858.4073→100858.4073 | `independent_verify.py`：reopen/volume/bbox/holes/drivers/feature_count/physical_hole_probe/cleanup 全 pass |
| DP-01 PDF→Part（模型 E2E） | 交付 sha256 `35cdde61…`，93919 B，14 checks PASS | 同上全 pass（spec `artifacts/model-pdf/independent-spec.json`） |
| DP-01 上传→PDF→Part（模型 E2E） | 上传 22323 B；交付 sha256 `dbea8e2d…`，93796 B，14 checks PASS | 同上全 pass（spec `artifacts/upload-pdf/independent-spec.json`） |
| A01 改型（MCP 直连） | `finalize COMPLETED`，7 checks pass，L 160→200，体积 190115.044408→238115.044408，孔心 ±65→±85 保边距 15 | `independent_verify.py`：source_sha256/reopen/volume/bbox/holes/drivers/feature_count/physical_hole_probe/original_untouched/cleanup 全 pass |
| A01 改型（模型 E2E） | 同上；交付 sha256 `064dffb0…`，75635 B | 同上全 pass（spec `artifacts/model-edit/independent-spec.json`） |
| 失败传播（真实 server） | 不完整 requirements 调 `catia_build_part` | 返回失败而非成功；`reportedCatiaStatus=CATIA_BUILD_UNCLASSIFIED`，`automaticRetryAllowed=false`；客户端被围栏 | `artifacts/real-catia/failure-propagation.json` |

真机证据要点：
- DP-01 体积 `92858.407346 mm³` 与闭式解 `96000 − 4π·5²·10` 一致；bbox `[-60,-40,0]..[60,40,10]`；孔心 `(±40,±20)`、R5。
- A01 改型后 `Part8\L=200`、`W=100`、`T=12`、`D=10`、`E=15`；体积 `238115.044408 mm³`；孔心 `(±85,35)` R5；源文件 sha256 未变。
- 模型 E2E 使用 `modelbest/deepseek-v4.1-flash-mb`；A01 回合 `inputTokens=107981`、`outputTokens=2870`；DP-01 回合 `inputTokens=232281`、`outputTokens=6167`。没有使用 Opus/GPT 或其他模型。

## 7. 关键实现说明

### 7.1 CATIA 识别不再写死 `catia_generation`

旧补丁只在 `spec.id === "catia_generation"` 时生效，而生产插件 id 是 `catia`。迁移后 `isCatiaServerId()` 接受 `catia` 与 `catia_*`；`readCatiaFailure()` 接受裸 `catia_build_part` 和 `mcp__catia__…`/`mcp__catia_generation__…`，拒绝其他 server 的同名工具。

### 7.2 结果安全

- build 返回 `isError=false` 但 envelope 不完整/`verification.verdict != PASS`/text 与 structuredContent 不一致 → 仍视为失败，`reportedCatiaStatus=CATIA_BUILD_UNCLASSIFIED`。
- build 超时、abort、session expired、结果畸形 → `catiaOutcomeUnknown=true`，之后该 client 的 `callTool/listTools/close` 都拒绝；需要重启 MCP server 并确认 CATIA 状态，不能靠“新连接”证明旧 build 已停止。
- `MATERIAL_COMPARISON_INCONCLUSIVE` 不触发“状态未知”围栏（模型已保存但未验证），但同样禁止自动重试。
- `CATIA_STATUS` JSON 注入模型可见错误文本；UI 只传经过白名单解析的 `reportedCatiaStatus` 字段，不透传原始大对象。

### 7.3 正斜杠 SaveAs 修复

`begin_edit` 的 `work_dir` 与 `finalize_edit` 的 `deliveryPath` 在 Windows 上可能来自模型/脚本的正斜杠路径。CATIA B31 会以裸 E_FAIL 拒绝，且旧代码静默回退到字节拷贝（`copied_by_server=true`）。修复后路径规范化，真实流程从“byte copy + finalize 失败”变为“NewFrom+SaveAs 成功 + finalize COMPLETED”。详见 `踩坑报告.md` 第 198 条。

## 8. 未解决问题、限制

1. **RunRegistry / run_events / 终端证据 / owned Gateway 未迁移**：新基座没有该子系统。影响：无按 runId 的事件重放、断线重连、签名终态；会话消息仍在。替代：独立 `independent_verify.py` + 本次证据文件。若需要恢复，应作为新基座的新子系统单独设计，而不是把旧文件硬塞进来。
2. **PDF 隔离未迁移**：新基座 `GatewaySubmitTurnInput` 没有 `allowedTools`/`sdkSessionConfig`/`systemPrompt`/每轮 plugin 字段，旧 `catiaPdfIsolation.js` 与 `prepare-generation-run.mjs` 无法直接运行。影响：没有自动 5 工具白名单、独立工作区和固定 system prompt；普通上传+技能路径已真机通过。替代：模型 E2E DP-01；若要恢复隔离，需要扩展新基座提交协议。
3. **CATIA 专用卡片/3D 预览未迁移**：旧卡片依赖旧 frontend business module 契约；新基座 UI 以通用工具 JSON 卡展示。影响：无 verdict/delivery 专属卡片和 WebGL 预览；交付路径/sha256/checks 仍可读。
4. **旧验收脚本族未迁移**：`owned-*`、`gateway-*-evidence`、`prepared-run-state`、`pdf-generation-isolation` 依赖旧 run registry/owned host/ORLA 数据集；本次以独立读回脚本替代，旧脚本保留在旧工作区未修改。
5. **浏览器 UI 点击流程未自动化**：UI 构建与 147 项组件/服务测试通过，网关事件流与模型回合已验证，但没有用 ego-browser 做“打开页面→选项目→发消息→截图”的端到端点击验收。
6. **既有基线失败**：13 个 Node 用例（Windows 路径/符号链接/PATH/FunASR/图片生成）和 1 个 MCP SDK 上下文用例在基线上就失败，本次未修复；Ruff 全量 622 条既有问题未清理。
7. **LFS**：51 个受 LFS 管理的文件中，文档图片/视频仍是指针；`ui/public/**` 图标已 `git lfs pull` 拉取，`scripts/computer-use/icon.png` 是真实 PNG（非指针）。若需要完整文档素材，运行 `git lfs pull`。
8. **`operationDeadlineMs` host 生产者未打通**：字段、`_meta` 和嵌套保护已就绪，但普通 UI 回合不设置 deadline；MCP `callTimeoutMs` 仍生效。

## 9. 切换与回退

- 新旧并存：旧 `AgentCPM` 与 `start-pilotdeck.cmd` 未改；新栈使用独立 `PILOT_HOME`、独立配置、端口 3001/5173/18789。迁移期间两套不同时启动。
- 切换到新栈：先停旧网关/旧 UI，再运行 `products/catia-agent/scripts/start-pilotdeck-migration.cmd`，确保 `MODELBEST_API_KEY` 已注入。
- 回退：停止新栈进程（node/CNEXT 中仅本次拉起的实例），重新运行旧 `F:\CatiaAgent\start-pilotdeck.cmd`。旧仓库、旧配置、旧 CATPart、旧运行记录均未改动。
- 新基座改动未提交、未推送；是否提交由用户决定。提交前建议按第 6 节命令复跑测试，并检查 `git status` 不包含 `dist/`、`node_modules/`、日志或凭据。

## 10. 证据索引

| 证据 | 路径 |
|---|---|
| 基线 git 状态/差异 | `baseline/agentcpm-status.txt`、`baseline/agentcpm-diffstat.txt`、`baseline/mcp-status.txt` |
| 插件/MCP 链路 | `logs/link-test.log` |
| DP-01 inspect 证据 | `artifacts/drawing-evidence-dp01/evidence.json`、`logs/inspect-dp01.log` |
| DP-01 直连构建 | `artifacts/real-catia/DP-01-build-result.json`、`DP-01-migration-report.json`、`logs/real-pdf-build.log` |
| DP-01 独立复核 | `artifacts/real-catia/DP-01-independent-spec.json`、`DP-01-independent-verify.json` |
| A01 直连改型 | `artifacts/real-catia/A01-{compile,apply,finalize}-result.json`、`logs/real-parametric-edit-5.log` |
| A01 独立复核 | `artifacts/real-catia/A01-independent-spec.json`、`A01-independent-verify.json` |
| 模型 E2E 改型 | `logs/e2e-model-edit.log`、`artifacts/model-edit/delivery.CATPart`、`artifacts/model-edit/independent-verify.json` |
| 模型 E2E PDF | `logs/e2e-model-pdf.log`、`artifacts/model-pdf/DP-01-model-delivery.CATPart`、`artifacts/model-pdf/independent-verify.json` |
| 上传→模型→PDF 建模 | `logs/e2e-upload-pdf.log`、`artifacts/upload-pdf/DP-01-upload-delivery.CATPart`、`artifacts/upload-pdf/independent-verify.json` |
| Gateway 回合 | `logs/e2e-gateway-2.log`、`logs/gateway-e2e.log` |
| 失败传播（真实 server） | `artifacts/real-catia/failure-propagation.json`、`logs/real-failure-propagation-2.log` |
| 构建/测试 | `logs/pilotdeck-build-2.log`、`logs/ui-build.log`、`logs/new-tests-dist.log`、`logs/pilotdeck-tests-targeted-2.log`、`logs/ui-vitest-targeted.log`、`logs/mcp-pytest-3.log`、`logs/mcp-ruff-2.log` |
| 能力清单 | `reports/capability-inventory.md` |
