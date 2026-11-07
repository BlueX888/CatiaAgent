# 第一阶段验收夹具

> **版本敏感**：目录里现有的 `.CATPart` 是 **R32 SP6** 保存的。CATIA 不能打开比自身 release 更新的
> 文件，所以在 R31（例如 `V5-6R2021 B31 SP7`）上会 `Documents.Open` E_FAIL、`catia_begin_edit`
> 直接 `SAVE_FAILED`（踩坑报告 83）。换到更旧的 release 时不要改这里，用
> `CATIA_FIXTURE_OUT=<其他目录>` 重建，再用 `CATIA_ACCEPTANCE_FIXTURES=<该目录>` 跑验收：
>
> ```powershell
> $env:CATIA_FIXTURE_OUT = 'F:\CatiaAgent\work\fixtures-r31'
> .\.venv\Scripts\python.exe tests\fixtures\build_fixtures.py A   # 同样跑 B / C / A-renamed
> .\.venv\Scripts\python.exe tests\fixtures\build_fixture_f.py
> # build_fixture_d.py / build_fixture_e.py 把输出写死在自己目录：先复制到 $env:CATIA_FIXTURE_OUT 再跑
> $env:CATIA_ACCEPTANCE_FIXTURES = $env:CATIA_FIXTURE_OUT
> .\.venv\Scripts\python.exe tests\acceptance\run_acceptance.py
> ```

七个 `.CATPart` 由本目录的构建脚本在真实 CATIA V5（上一台机器 R32 SP6，中文界面）上生成，
不是手工文件。删除后用下面的命令重建（需要一个正在运行的 CATIA 实例）：

```powershell
cd F:\CatiaAgent\catia-v5-mcp-server
.\.venv\Scripts\python.exe tests\fixtures\build_fixtures.py A
.\.venv\Scripts\python.exe tests\fixtures\build_fixtures.py B
.\.venv\Scripts\python.exe tests\fixtures\build_fixtures.py C
.\.venv\Scripts\python.exe tests\fixtures\build_fixtures.py A-renamed
.\.venv\Scripts\python.exe tests\fixtures\build_fixture_d.py
.\.venv\Scripts\python.exe tests\fixtures\build_fixture_e.py
.\.venv\Scripts\python.exe tests\fixtures\build_fixture_f.py
```

## 共同几何

平板 `L × W × T`，中心在 XY 原点（X ±L/2、Y ±W/2），底面 Z=0。
初始 L=160、W=100、T=12、孔径 D=10、孔边距 E=15。
两个 Ø10 贯穿孔位于 `(±(L/2−E), W/2−E)`，即 `(±65, 35)`。

拓扑：一个草图（矩形 4 条线 + 2 个圆）→ 一个 Pad；没有独立孔特征。

## 六个夹具的差别（决定它们能证明什么）

| 文件 | 孔位置约束 | 用途 |
|---|---|---|
| `FixtureA-mounting-plate.CATPart` | 每个孔用**到自身最近矩形边**的最小距离尺寸定位，边距尺寸 = `E − D/2` 公式驱动 | 改 L/W 时孔随边界移动，边距保持；A01–A05、A10、A11、A12、A13、A14 |
| `FixtureB-fixed-holes.CATPart` | 孔用**到固定构造轴**的尺寸定位（字面值），另有 `UnusedParam=42` 不驱动任何几何 | 拒绝"保持边距"（A08）、NO_EFFECT（A09） |
| `FixtureC-no-holes.CATPart` | 无孔 | 测量不足 → INCONCLUSIVE（A09） |
| `FixtureA-renamed.CATPart` | 同 A，但用户参数改名为 `PlateLength/PlateWidth/PlateThickness/HoleDiameter/EdgeDistance` | 中英文重命名后可读（T2） |
| `FixtureD-ambiguous.CATPart` | 长度由 `P1 + P2` 共同驱动（各 80 mm），参数名不含长宽语义 | 方向/参数不唯一时任何猜测都不是唯一解；验收 A06 证明编造/歧义 handle 在写入前被拒 |
| `FixtureE-noparam.CATPart` | 只有实体与草图几何，**没有任何尺寸约束/用户参数** | 非参数化实体：`driver_handles()==[]`，`begin_edit` 必须返回 `UNSUPPORTED_CAPABILITY`（A07） |

参数全部由用户参数 + 公式驱动，因此参数树里能看到 `L/W/T/D/E` 与公式关系。
孔位置依赖用的是**几何约束**（尺寸指向会移动的边），不是引用 L/W 的公式；
服务端的能力判定因此以**实测扰动**为准（见 `catia_mcp/editing/session.py::probe_dependencies`）。

## 参考值（独立于实现的闭式解）

| 状态 | 体积 mm³ | 包围盒 |
|---|---|---|
| 初始（160×100×12，2×Ø10） | `(160·100 − 2π·5²)·12 = 190115.044408` | X±80、Y±50、Z0–12 |
| L=200 | `(200·100 − 2π·25)·12 = 238115.044408` | X±100、Y±50、Z0–12，孔 (±85,35) |
| L=260 | `(260·100 − 157.0796)·12 = 310115.044408` | X±130，孔 (±115,35) |
| L=260,W=120 | `(260·120 − 157.0796)·12 = 372515.044408` | Y±60，孔 (±115,45) |
| L=260,W=120,T=15 | `(31200 − 157.0796)·15 = 465643.805510` | Z0–15 |
| D=12（160×100×12） | `(16000 − 2π·36)·12 = 189285.663947` | 孔半径 6 |

体积公式：`体积 = (L·W − 孔数·π·(D/2)²)·T`。
容差见 `docs/editing-protocol-v1.md` §1：长度/坐标 ±0.01 mm，体积 `max(0.01 mm³, 期望×1e-6)`。

## Fixture F（P1/P2 结构语义）

`FixtureF-boss-and-side-hole.CATPart` 由 `build_fixture_f.py` 在真实 CATIA 上生成，用于：

- **孔 vs 凸台**：一个凸台圆是 `additive_feature`（半径增大时体积增大），两个安装孔和一个侧面孔是 `hole`（半径增大时体积减小）；判定来自逐 driver 扰动，不靠圆的存在或名称。
- **非 XY 草图**：侧面孔的草图在 `PlaneYZ`，`GetAbsoluteAxisData` 给出 `H=(0,1,0)`、`V=(0,0,1)`；孔心局部 `(0,6)` 映射到模型 `(0,0,6)`。
- **多组孔边距**：`草图.1` 的安装孔随 L/W 移动、边距保持 15；中心侧面孔的边距随 L/W 改变，所以全局 `holes_edge_distance` 为 false，但 `scopedGroups` 里 `草图.1` 为 true，可用带 `sketch` 作用域的 preserve。
- **凸台圆心稳定**：凸台圆只约束半径，不用“圆到直线距离=0”（那个约束是最小距离，会让圆相切并随半径移动，见踩坑 79）。

参数：`L=160, W=100, T=12, D=10, E=15, R=24, BH=20, S=10`。
闭式体积：`(L·W − 2π(D/2)²)·T + π(R/2)²·(BH−T) − π(S/2)²·L`（侧面孔是 ±200 对称通槽，L≤400 时贯通；不是只挖 200）；初始 `181167.788521 mm³`，
包围盒 `X±80、Y±50、Z0–20`，圆：两安装孔 `(±65,35) r5`、凸台 `(0,0) r12`、侧面孔世界 `(0,0,6) r5`。
