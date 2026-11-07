---
description: 只读核对 CATIA 编辑会话的状态、revision、检查项与未知或失败操作
argument-hint: <editSessionId>
---

# /catia-edit:verify-part

只读核对一个 CATIA 编辑会话。**本命令不得写入**：不调用 `catia_apply_edit_plan`、`catia_finalize_edit`、`catia_recover_edit`，也不调用任何通用写工具。

## 1. 取会话 id

从命令参数里取 `editSessionId`（`catia_begin_edit` 返回的 12 位十六进制串）。没有就向用户要；不要猜，也不要用"最近一个会话"代替。

## 2. 调用（只读）

1. `mcp__catia__catia_inspect_edit`，入参 `{"editSessionId":"<id>"}`
2. `mcp__catia__catia_get_edit_status`，入参 `{"editSessionId":"<id>"}`

## 3. 报告

按下面字段输出，缺哪个就写"未提供"，不要补造：

- 会话：`data.editSessionId`、`data.businessState`、`data.modelRevision`
- 文档：`data.documentOpen`。若 `code` 是 `DOCUMENT_CLOSED`，说明工作副本未在 CATIA 中打开、状态来自 journal，继续写入需要重新 `catia_begin_edit`。
- 最近结果（`catia_get_edit_status` 的 `data.lastResult`）：`operationId`、`verdict`、`code`、`modelRevision`、`utc`；会话还没有结果时写"未提供"。
- 检查项：`data.lastResult.checks[]` 的 `id`、`status`、`evidence`；把 `status` 不是 `pass` 的全部单列。
- 信封消息：两个工具顶层各自的 `message` 原样给出。
- 操作：`data.operations[]` 的 `operationId`、`state`、`planHash`、`updatedUtc`；把 `failed`、`unknown`、`intent`（只有意图、无证据）单列。
- 事件：`data.events` 里 `rollback`、`rollback_failed`、`source_changed`、`session_abandoned`、`recover` 这类关键事件。
- 结论：该会话处在状态机哪一步（`ready`/`executing`/`verified`/`saving`/`completed`/`failed`/`unknown`/`recovering`/`rolled_back`），是否已有磁盘交付。只有 `businessState:"completed"` 才表示交付文件已保存并通过重开验证。

## 4. 规则

- `ok` 不代表健康：`catia_inspect_edit` 可能 `ok:true` 但 `code:"DOCUMENT_CLOSED"`；`catia_get_edit_status` 的 `ok:true` 也只表示读到状态，不代表模型通过验证。
- 两个工具说法不一致时分别说明：实时快照以 `catia_inspect_edit` 为准，历史操作以 `catia_get_edit_status` 为准，不要合并成一个看似确定的结论。
- `ok:false` 或 `code:"NOT_FOUND"` 时原样给出 `code` 和 `message`。
- 若会话状态是 `unknown`/`recovering`，或 `data.operations[]` 中存在 `intent`/`unknown` 操作，建议下一步 `catia_recover_edit`（先 `dryRun:true`），但**不要执行**。`executing`/`saving` 表示写入可能正在进行：只报告，不要动，也不要用通用写工具。
- 不写文件、不改 CATIA、不给"已验收/已改好"的结论；只报告证据。
