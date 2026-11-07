---
description: 从 PDF 工程图创建参数化 CATPart（先读图取证，缺信息一次问全）
---

用户要用 PDF 工程图从零创建零件。按 `catia-pdf-model` 技能执行：

1. 在本轮上下文里找到已登记附件的绝对路径（`[Registered attachment files in this session:]`）；用户消息里给了路径就直接用。
2. `catia_inspect_drawing` 读图，记录 `evidencePath` 与关键尺寸 span。
3. 写 `catia-build-requirements/1`，每个尺寸引用真实 span id。
4. `catia_validate_requirements`：`needs_clarification` 时把问题**一次性**问完，零写入；用户回答后补 `clarifications` 再校验。
5. `catia_compile_build_plan`，不要改返回的 plan。
6. `catia_build_part` 保存到新的 `.CATPart` 路径（默认 `F:\CatiaAgent\runs\pdf-modeling\<日期>\<图号>-<零件名>.CATPart`），`editCheck:true`。
7. 只有 `businessState:"completed"` + `verification.verdict:"PASS"` + `editability.passed:true` 才汇报完成；否则说明失败阶段和原因。

