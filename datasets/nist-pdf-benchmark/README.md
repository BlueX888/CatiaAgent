# NIST PDF 建模基准（17 例）

日期：2026-10-28  
方案：[CATIA_Agent_NIST全量实验与能力扩展开发方案.md](../../CATIA_Agent_NIST全量实验与能力扩展开发方案.md)

本目录是 NIST 17 个独立零件的**验收输入与冻结期望**。生成侧只能读 PDF；本目录中的
`verification/`、参考模型和运行结果不得进入建模会话。

## 目录

| 路径 | 内容 | 谁能读 |
| --- | --- | --- |
| `manifest.json` | 17 例的原始 PDF、参考模型、哈希、输入审查结论 | 验证侧/审计 |
| `case-review.md` | 逐例图纸审查、能力缺口、阻塞项 | 验证侧/审计 |
| `requirements/<case>.json` | 已记录的读图结果（含 span/OCR 引用） | 建模侧/回放 |
| `verification/<case>.json` | 冻结的体积、包围盒、圆柱面、参数和容差 | **仅验证侧** |
| `tools/build_manifest.py` | 由固定表生成 manifest | — |
| `tools/run_pdf_baseline.py` | 对 17 份原始 PDF 跑文本 + OCR 读取基线 | — |

派生 PDF 尚未制作，计划放 `derived/`，并在文件名和首页标注来源、补充内容和版本。

## 当前状态（20261028 批次）

- 17/17：原始 PDF 已登记、哈希、读取基线已跑。
- **1/17（FTC-11）完成**：原始 PDF → AgentCPM 自己 OCR 读图 → 圆环建模 →
  两次最终运行（run3 真实 UI 上传 + run4 同会话批量入口）→ 链路校验与独立参考校验均 PASS。
- 16/17：未完成。FTC/CTC 缺派生尺寸图；STC-06～10 的参考已用对应 FTC 等价几何解锁；
  HTC 的参考 CATPart 是 V5-6R2023，本机 B31 SP7 打不开。
- 参考特征树清单：`feature-inventory.md`（验证侧）。
- 全量结果与证据：`runs/nist-pdf-benchmark/20261028/`。

## 生成/验证隔离

- 生成侧输入：原始或派生 PDF、`catia_inspect_drawing` 产出的证据 manifest。
- 生成侧禁止：`datasets/NIST/models/**`、`verification/**`、历史 CATPart、本目录的审查结论。
- 独立校验：重开磁盘上的交付件，用冻结期望测量；不使用建模进程自报的结果。

## 重新生成

```bash
catia-v5-mcp-server/.venv/Scripts/python.exe datasets/nist-pdf-benchmark/tools/build_manifest.py
catia-v5-mcp-server/.venv/Scripts/python.exe datasets/nist-pdf-benchmark/tools/run_pdf_baseline.py
```
