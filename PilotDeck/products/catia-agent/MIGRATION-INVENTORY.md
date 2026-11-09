# PilotDeck 迁移能力清单（旧工作区 → 新基座）

- 旧基线：`F:\CatiaAgent\AgentCPM`，HEAD `d5bd96a714fd1feb914aa9ff8a62d65f69eb41e8`，分支 `p1p2-inject-user-text`；工作区 46 个已跟踪修改文件 + 60 个未跟踪文件（含 `products/catia-agent` 验收脚本）。
- 新基座：`F:\CatiaAgent\PilotDeck`，HEAD `32a291f65417af9358a54e13072f5a8a1752ad18`，`main` 浅克隆。
- MCP server：`F:\CatiaAgent\catia-v5-mcp-server`，HEAD `8a87e7c7c519cfdd0aea5e50b04651db66340e5e`，分支 `p1p2-structure-context`（工作区另有既有未提交修改，本次只加 2 处路径规范化和 1 个测试）。

状态说明：**新版已有** = 新基座原生具备；**直接迁移** = 文件/语义可原样或近原样搬入；**适配** = 需要改接口/路径/语义；**暂不迁移** = 依赖新基座不存在的子系统，列出影响和替代。

## 一、CATIA 产品与插件

| 能力 | 来源（旧工作区） | 目标 / 迁移方式 | 依赖 | 测试/证据 | 状态 |
|---|---|---|---|---|---|
| `catia-edit` 插件声明 | `products/catia-agent/plugins/catia-edit/plugin.json` | `PilotDeck/products/catia-agent/plugins/catia-edit/plugin.json`；MCP 命令/环境改为 `${env:...}` 占位符；增加 `concurrencySafe:false` | 新基座 `PluginRuntime` + `parsePluginMcpServers` | `link-test.log`：插件发现、2 技能、2 命令、`catia` server | 适配 |
| `catia-parametric-edit` 技能 | 同上 `skills/catia-parametric-edit/SKILL.md` | 原样复制 | 11 个 CATIA 编辑工具 | 模型 E2E 加载技能并完成 A01 | 直接迁移 |
| `catia-pdf-model` 技能 | 同上 `skills/catia-pdf-model/SKILL.md` | 原样复制（含最新 ORLA/组合特征说明） | PDF 工具链 | 模型 E2E 完成 DP-01 | 直接迁移 |
| `/catia-edit:pdf-model` 命令 | `commands/pdf-model.md` | 原样复制 | 新基座 `InputProcessor` 插件命令 | 模型 E2E 使用技能；`commands_list` 不列插件命令（见限制） | 直接迁移 |
| `/catia-edit:verify-part` 命令 | `commands/verify-part.md` | 原样复制 | 编辑工具 | 插件加载清单含该命令 | 直接迁移 |
| 插件安装脚本 | `scripts/install-plugin.ps1` | 重写为只接受显式 `PILOT_HOME`、逐文件复制、不使用 `robocopy /MIR`、可选 `-CopySkills` | PowerShell 5.1 | 已实际安装到迁移 home | 适配 |
| 前端 profile | `profiles/catia-edit.frontend.yaml` | 不迁移 | 旧 `scripts/generate-frontend-modules.mjs` 在新基座不存在 | — | 暂不迁移 |
| 产品 README | `products/catia-agent/README.md` | 重写为新基座安装/环境/差异说明 | — | `products/catia-agent/README.md` | 适配 |

## 二、核心运行时可靠性

| 能力 | 旧实现 | 新基座现状 | 迁移结果 | 测试/证据 | 状态 |
|---|---|---|---|---|---|
| MCP `structuredContent` 透传 | 旧 `McpClient`/`McpRuntimePort` 新增字段 | 新基座已原生透传，并在工具结果中追加 `json` block | 无需迁移 | `src/mcp/client/McpClient.ts:258` | 新版已有 |
| 非幂等工具不盲目重放 | 旧 `McpClient` 对 CATIA build 禁止重放 | 新基座按 MCP annotations `readOnlyHint/idempotentHint` 决定是否重放 | 已有更通用实现 | `McpClient.callWithReconnect` | 新版已有 |
| CATIA build 结果形状校验 | 旧 `catiaFailureStatus.ts` + `PluginToToolBridge` | 新基座无 CATIA 专用校验 | 迁移 `src/tool/protocol/catiaFailureStatus.ts`；bridge 在 build 成功/失败时校验 `ok/code/businessState` 与 text/structuredContent 一致 | `tests/mcp/catia-build-failure.spec.ts` 9 项 | 适配 |
| CATIA 失败状态透传 | 旧 `reportedCatiaStatus` + recovery + projector | 新基座无 | 迁移到 `errorRecovery.ts`、`InProcessGateway.ts`、`ui/server/pilotdeck-bridge.js`；`CATIA_STATUS` 注入模型可见错误文本 | 19/19 新测试；UI bridge 测试 | 适配 |
| 结果未知/忙碌围栏 | 旧 `McpClient.catiaOutcomeUnknown/catiaBuildInFlight`，含 `close/listTools` 拦截 | 新基座无 | 迁移到 `McpClient`，server id 识别改为 `catia`/`catia_*`；保留“重启 MCP server 才是解除围栏”的语义 | `catia-session-fence.spec.ts` 3 项 | 适配 |
| CATIA `_meta` 操作 deadline | 旧 `operationDeadlineMs` + `_meta` | 新基座无 host deadline 生产者 | 新增 `PilotDeckToolRuntimeContext.operationDeadlineMs`，bridge 对 build 传 `_meta["pilotdeck/operationDeadlineMs"]`；嵌套工具不能覆盖（`ToolRuntime`） | `catia-session-fence.spec.ts` 校验 `_meta` | 适配（无副作用；当前无 host 生产者） |
| 空回复/输出上限续写提示 | 旧统一提示“无可见文本或工具调用，继续工作/使用工具” | 新基座有 7 处旧式提示 | 统一为 `EMPTY_RESPONSE_RETRY_PROMPT` | `AgentLoop.ts` | 直接迁移 |
| 推理中断重试 | 旧 `reasoning_restart` + 推理缓冲 + UI 状态 | 新基座会把 reasoning 阶段中断直接报错 | 迁移推理缓冲/整请求重试/`reasoning_restart` 事件；UI 增加重试原因中英文案 | `streamModel.ts`、`MessagesPaneV2.tsx`、i18n | 适配 |
| 嵌套工具 deadline 不可覆盖 | 旧 `ToolRuntime` 保留 `operationDeadlineMs` | 新基座可被 `contextPatch` 覆盖 | 已迁移 | `ToolRuntime.ts` | 直接迁移 |
| 工具失败恢复建议 | 旧 CATIA 专项 advice | 新基座有通用 `buildToolErrorRecovery` | 在通用逻辑前加 CATIA 分支 | `errorRecovery.ts` | 适配 |
| RunRegistry（文件批量写、SQLite、迁移、选择开关） | 旧 `RunRegistry.ts`/`SqliteRunRegistry.ts`/`selectRunRegistry.ts`/`migrate-run-registry.py` | 新基座没有 RunRegistry、`run_events`、`run_reattach`、run timing | **不迁移** | — | 暂不迁移 |
| 终态证据/签名/网关身份 IPC | 旧 `TerminalEvidenceAuthority`/`gatewayIdentityIpc`/`recordOwnedTerminalEvidence`/`collectOwnedTerminalEvidence` | 新基座没有对应传输与方法 | **不迁移** | — | 暂不迁移 |
| Run 事件 gap 检测/preflight 终态 | 旧 `InProcessGateway` 补丁 | 新基座无 run registry/run events | **不迁移** | — | 暂不迁移 |

## 三、UI / 上传 / 隔离

| 能力 | 旧实现 | 新基座现状 | 迁移结果 | 证据 | 状态 |
|---|---|---|---|---|---|
| 通用文件上传（PDF/STEP/JSON） | 旧 `UploadStore` + 路由 | 新基座 `UploadStore` + `/api/uploads` 路由，支持任意文件与完整性校验 | 无需迁移 | `ui-vitest-targeted.log` uploads 测试 | 新版已有 |
| 附件绝对路径注入模型上下文 | 旧 skill 依赖 `[Registered attachment files...]` | 新基座 `InProcessGateway` 已注入 | 无需迁移 | `ATTACHMENT_PATH_NOTE_MARKER` | 新版已有 |
| CATIA 工具结果 UI 透传 | 旧 `gatewayEventToFrames` 增加 `reportedCatiaStatus` | 新基座无 | 迁移事件字段；retry 文案增加 `reasoning_restart` | UI 147/147 | 适配 |
| CATIA build 卡片/3D 预览 | 旧 `composition/modules/catia-edit.tsx`（839 行）+ `catia-preview.tsx` | 新基座没有旧 frontend business module 生成器/`SurfaceProps` 契约 | **不迁移**，通用工具 JSON 卡片 + 模型总结展示交付路径/sha256/verdict | 模型 E2E 最终文本 | 暂不迁移 |
| PDF 隔离生成（固定 5 工具、独立工作区、固定 system prompt、每轮临时插件） | 旧 `ui/server/services/catiaPdfIsolation.js` + `acceptance/prepare-generation-run.mjs` + `run-prepared-generation.mjs` | 新基座 `submitTurn` 没有 `allowedTools`/`sdkSessionConfig`/`systemPrompt`/每轮 plugin 字段；也没有旧 run registry/owned host | **不迁移**；普通上传 + `catia-pdf-model` 技能路径已真实验证 | `e2e-model-pdf.log` | 暂不迁移 |
| Owned CATIA UI / owned Gateway / 阈值策略 | 旧 `ownedCatiaUi.js` + `owned-gateway-host.mjs` + `threshold-commitment` | 依赖旧 owned host/终端证据/run registry | **不迁移** | — | 暂不迁移 |
| 旧验收脚本族（`owned-*`、`gateway-*-evidence`、`prepared-run-state`、`pdf-generation-isolation`） | `products/catia-agent/acceptance/*` | 依赖上述旧子系统与 ORLA 数据集 | **不迁移**；改用 MCP server 自带 `tests/acceptance/independent_verify.py` + 本次新增证据 | 独立验证 JSON | 暂不迁移 |

## 四、MCP server 改动

| 改动 | 文件:行 | 原因 | 测试/验证 |
|---|---|---|---|
| `deliveryPath` 在冲突检查/SaveAs 前 `normpath` | `catia_mcp/editing/coordinator.py:1547` | CATIA B31 `SaveAs` 拒绝正斜杠路径，只抛裸 E_FAIL | 真机 A01 finalize `COMPLETED`；独立复核全 pass |
| `work_dir` 在 `create_working_copy` 前置 `abspath` | `catia_mcp/editing/session.py:94` | 正斜杠工作目录导致 `NewFrom+SaveAs` 被拒并静默回退字节拷贝 | 新增单测 `test_forward_slash_work_dir_is_normalized_before_save_as`；MCP pytest 773 pass |
| 新增正斜杠回归测试 | `tests/test_editing_working_copy.py:95` | 固定上述行为 | 通过 |

## 五、明确未迁移项的影响与替代

1. **运行记录/重放/签名终态**：新基座没有 `RunRegistry`/`run_events`/terminal evidence。影响：刷新或断线后不能按 runId 重放事件，不能取得宿主签名终态；会话消息与磁盘交付仍保留。替代：模型/工具事件由 Gateway 流式消费，交付件用独立 `independent_verify.py` 读回验收；迁移报告保留完整证据文件。
2. **PDF 隔离**：影响：没有自动 5 工具白名单、独立工作区和固定 system prompt；模型可调用其他工具，工作区由普通会话决定。替代：普通上传 + `catia-pdf-model` 技能 + 工作区路径约定；真机 DP-01 模型回合已通过。若必须恢复隔离，需要先在新基座 `GatewaySubmitTurnInput`/AgentSession 上新增 allowlist/系统提示/每轮插件能力，属于新子系统，不在本次迁移范围。
3. **CATIA 专用卡片/3D 预览**：影响：UI 以通用工具结果卡显示 JSON，用户需要读模型总结或展开原始结果；无 WebGL 预览。替代：交付路径、sha256、verdict、checks 都在结果 JSON 与模型总结中；可用 CATIA 打开交付件。
4. **旧验收脚本族**：影响：不能直接复跑旧 `owned-*`/prepared-run 脚本。替代：本次用 MCP server `independent_verify.py` 和新写的 MCP 直连脚本完成真机验收；旧脚本保留在旧工作区，未修改。
5. **`operationDeadlineMs` host 生产者**：影响：当前只有测试/宿主显式传值时才把 deadline 写进 `_meta`，普通 UI 回合不传。MCP server 的 `callTimeoutMs`（30 分钟）仍然生效；deadline 只是“排队前提前拒绝”的额外保险。替代：后续可在 `GatewaySubmitTurnInput.timeoutMs` → AgentSession → `createToolContext` 打通。
