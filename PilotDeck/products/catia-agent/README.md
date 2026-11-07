# catia-agent（新 PilotDeck 基座迁移版）

CATIA V5 产品包。一个插件 `catia-edit` 下有两个技能：

| 技能 | 输入 | 做什么 | 命令 |
| --- | --- | --- | --- |
| `catia-edit:catia-parametric-edit` | 已保存的单实体参数化 `.CATPart` + 自然语言改型要求 | 按 `catia-edit/1` 编辑协议改尺寸（begin → 意图编译 → apply → finalize） | `/catia-edit:verify-part` |
| `catia-edit:catia-pdf-model` | 单个零件的 PDF 工程图 | 读图取证 → 结构化需求（带 span 引用）→ 受控建模方案 → CATIA 从零建模 → 独立校验 → 交付 | `/catia-edit:pdf-model` |

两条链路互斥路由：PDF 图纸新建零件不要走改型技能；改已有 Part 也不要走 PDF 技能。

## 迁移来源与目标

- 目标基座：`F:\CatiaAgent\PilotDeck`，`git rev-parse HEAD` = `32a291f65417af9358a54e13072f5a8a1752ad18`（`main` 浅克隆）。
- 旧工作区基线：`F:\CatiaAgent\AgentCPM`（`d5bd96a714fd1feb914aa9ff8a62d65f69eb41e8` 加未提交修改），只读参考，不修改。
- MCP server：`F:\CatiaAgent\catia-v5-mcp-server`（`8a87e7c7c519cfdd0aea5e50b04651db66340e5e` 加未提交修改），作为现有接口使用。
- 迁移报告：`F:\CatiaAgent\work\pilotdeck-migration-20261105\reports\migration-report.md`。

## 安装 / 启用

新基座的插件发现目录是 `<PILOT_HOME>\plugins` 与 `<项目>\.pilotdeck\plugins`。迁移版要求显式设置独立 `PILOT_HOME`，不会写入用户默认的 `%USERPROFILE%\.pilotdeck`：

```powershell
$env:PILOT_HOME = 'F:\CatiaAgent\work\pilotdeck-migration-20261105\home'
powershell -ExecutionPolicy Bypass -File products\catia-agent\scripts\install-plugin.ps1
# 可选：-CopySkills 把两份 SKILL.md 再复制到 $PILOT_HOME\skills，供 Web UI 技能页显示；
# 插件运行时本身已加载插件内技能，复制会产生第二个同名独立技能条目。
```

插件按文件覆盖同名目标，不使用 `robocopy /MIR`，不会删除用户在插件目录里新增的文件。

## 运行时环境

插件 `plugin.json` 使用 MCP 占位符，由网关进程环境展开：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `CATIA_MCP_PYTHON` | 是 | `F:\CatiaAgent\catia-v5-mcp-server\.venv\Scripts\python.exe` |
| `CATIA_MCP_WORK_DIR` | 是 | CATIA 中间产物目录，建议放在迁移工作区 |
| `CATIA_CAD_SUPPORT_PYTHON` | 否 | 带 `build123d` 的独立 Python，用于预览/参考几何 |
| `CATIA_OCR_PYTHON` | 否 | PDF OCR 使用的 Python；缺省时用 MCP server 自身解释器 |
| `MODELBEST_API_KEY` | 是 | 模型密钥，只从环境读取，不写入仓库 |

配置模板：`products/catia-agent/config/pilotdeck.yaml`。模型固定为 `modelbest/deepseek-v4.1-flash-mb`，没有 Opus/GPT 回退。

## MCP server

插件声明 `mcpServers.catia`，工具 wire name 为 `mcp__catia__catia_*`。同名 `mcp.json` 配置优先于插件配置（与旧基座一致）。

`callTimeoutMs` 为 `1800000`（30 分钟）：PDF 全链建模 + 独立校验 + 可编辑性检查通常 10–20 s，但整图建模含冷启动与 CATIA 忙碌时会远超 MCP 默认的 60 s。

## 关闭 / 卸载

删除 `$PILOT_HOME\plugins\catia-edit` 后等待扩展重载。新基座 `extension.builtinPluginsEnabled` 只关内置插件，对本插件无效。

## 环境与范围

- Node.js `>=22.13.0 <23`（新基座 `engines` 与 `scripts/check-node-runtime.mjs` 会拒绝 24）。
- 改型支持：已保存的**单实体参数化 `.CATPart`**，改写长度、宽度、厚度、孔径、孔边距等驱动参数，并做交付保存 + 磁盘重开验证。
- PDF 建模支持：单零件、平面板件/圆环/闭合轮廓/组合特征；优先带文本层的 PDF，矢量轮廓可用 OCR。
- 不支持：新增特征（改型）、装配、自由曲面/GSD、无参数导入实体、折弯/曲面钣金、扫描图无尺寸逆向。

## 验收证据（2026-11-05）

- 迁移报告：`MIGRATION-REPORT.md`；逐项能力清单：`MIGRATION-INVENTORY.md`。
- 真实 CATIA 模型回合（DeepSeek）：参数化改型交付 `delivery.CATPart`（sha256 `064dffb0…`），独立读回全 pass；PDF 建模交付 DP-01（120×80×10，4×Ø10，sha256 `35cdde61…`），独立读回全 pass；上传附件→PDF 建模交付 `DP-01-upload-delivery.CATPart`（sha256 `dbea8e2d…`），独立读回全 pass。
- 证据目录：`F:\CatiaAgent\work\pilotdeck-migration-20261105\logs\` 与 `artifacts\`；独立读回使用 `catia-v5-mcp-server/tests/acceptance/independent_verify.py`。

## 与旧基座的已知差异

1. 新基座没有旧版 `sdkSessionConfig` / `allowedTools` / `systemPrompt` 提交字段，因此旧版“PDF 隔离生成”（固定 5 工具白名单、独立工作区、固定 system prompt、每轮临时插件）**没有迁移**。PDF 建模仍可在普通会话中通过上传 PDF + `catia-pdf-model` 技能完成，但没有自动工具白名单和工作区隔离。见迁移报告。
2. 新基座没有旧版 RunRegistry / `run_events` / terminal evidence / owned Gateway 子系统，因此旧版 owned-run 验收脚本、签名终态证据、SQLite run registry **没有迁移**。会话与消息仍由新基座持久化；CATIA 交付件验收改用独立读回脚本（MCP server `tests/acceptance/independent_verify.py`）。
3. 新基座没有前端业务模块生成器（旧 `scripts/generate-frontend-modules.mjs`），旧 `profiles/catia-edit.frontend.yaml` 不再适用。CATIA 构建卡片/三维预览尚未移植；工具结果仍以通用 JSON 卡片展示，交付路径和 sha256 可读。
4. 已迁移的运行时可靠性：CATIA build 结果形状校验、失败状态透传（`reportedCatiaStatus`）、结果不确定时客户端围栏、嵌套工具不覆盖主机 deadline、推理中断重试（`reasoning_restart`）。
