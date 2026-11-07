# text-to-cad 能力集成

目标：保留 AgentCPM → CATIA 原生参数化建模，将 text-to-cad 的组合建模设计、工程图生成与几何检查用于现有链路。

## 实施与验收清单

- [ ] 可组合特征需求、证据校验、计划、CATIA 执行与参数化验证；保留已有板件、圆环与轮廓接口。
- [ ] 独立环境中的 STEP → 派生 PDF 工具，记录来源，禁止覆盖原文件；标注与视图由显式规格定义。
- [ ] 独立 STEP 几何诊断与参考比对，错误及不确定状态不能判定通过。
- [ ] PDF 证据 → 特征 → CATIA 对象 → 验收结果的追溯，以及有边界的错误反馈。
- [ ] 生成件的三维预览入口，预览不替代 CATPart 及工程验收。
- [ ] 离线契约/失败测试、真实几何工具测试、真实 CATIA 测试及 AgentCPM 用户链路验证。

## 边界

制图与参考比对只在数据准备/验证侧运行，生成 Agent 不得读取 NIST 参考模型或验收答案。制图工具不自动保证尺寸齐全；派生 PDF 需要独立审查，不能算作官方原始图纸成功。

运行 Open CASCADE 的辅助进程使用独立环境，与 CATIA COM 进程隔离。外部依赖锁定版本，导入的模块不执行模型脚本。

本清单记录当前实现任务，不代表功能已完成或 NIST 全部已通过。

## 2026-10-28 续接记录：辅助工具

已通过真实 OCCT/PDF/OCR 回归：`tests/test_cad_support.py`、
`tests/test_cad_support_client.py`、`tests/test_drawing_pdf_evidence.py`，
共 32 项通过（26.88 秒）。制图测试通过实际 `inspect_pdf(include_ocr=True)`
读回 40、30、8 mm 和 DERIVED 标记，同时验证来源哈希与禁止覆盖。
图面人工检查输出：`F:/CatiaAgent/work/cad-support-plate.png`。

独立环境依赖见 `requirements-cad-support.txt`；当前实装 cadgen 0.8.1、
PyMuPDF 1.28.2、rapidocr-onnxruntime 1.4.4。不要将 OCCT 安装进 COM 环境。

准备/验收脚本可调用 `catia_mcp.cad_support_client.run_support(operation, spec)`，
设置 `CATIA_CAD_SUPPORT_PYTHON` 为独立 Python 绝对路径，或传 `python=`。
支持 draw/inspect/compare，默认超时 120 秒，检查退出码和 JSON 协议，保留
最多 4000 字符 stderr；超时不自动重试，先检查是否已生成文件。
它不注册为生成 Agent 可用的 MCP 工具。

正式读图进程可以设置 `CATIA_OCR_PYTHON` 指向同一独立 Python；未设置时沿用
当前解释器。此配置已实现，尚未修改正在运行的服务环境或重启服务。
OCR 使用更高检测分辨率并补充顺时针 90° 识别，将框映射回原图坐标；
不同方向可能产生重复或冲突读数，仍须核对图面，不能把 OCR 当作工程真值。

尚未完成：实际 NIST 派生图独立审查、组合特征主链路和执行器、特征追溯、
预览、真实 CATIA/AgentCPM 验收以及 NIST 全量实验。上方总体验收清单保持未完成。

## 2026-10-28 续接记录：组合特征编译器契约

`feature_model.py` 已补充对象类型、未知字段、按操作/轮廓区分字段、唯一引用、
重复证据 ID、简单标识符与参数范围检查。公式系数不再截短为 6 位有效数字；
计划中的依赖和参数证据列表不共享输入列表。

新增 `tests/test_feature_model.py`，先复现 9 项失败再修复。
执行 `.venv/Scripts/python.exe -m pytest tests/test_feature_model.py tests/test_drawing_requirements.py -q`
得到 **63 passed，0.26 秒**。覆盖主体 + 加料 + 切除计划、inch 转换、证据不匹配、
重复/前向/循环依赖、表达式注入、量纲混算、除零、负轮廓尺寸、异常嵌套结构、
字段冲突、公式精度和旧需求回归。

这仍是编译器契约验证。几何轮廓相交/单实体语义、旋转轴侧别、CATIA 公式语法、
参数传播和实际切除效果未由这些测试证明。新 schema 尚未接入 MCP、执行器、
独立验收或 Agent 技能；下步需贯通这些路径，不能将上述 63 项作为建模完成证据。

## 2026-10-28 续接记录：组合特征执行器与真实探针

新增 `drawing/feature_executor.py`，在调用者持有 STA/机器锁的前提下新建文档，
执行参数、偏移支撑平面、约束化矩形/圆草图、Pad/Pocket/Shaft/Groove；
每个特征独立命名公式，记录证据、对象名、体积变化和包围盒。
执行前重新计算计划表达式，拒绝伪造数值；材料无变化、方向错误或测量无效即停止。
圆心定位使用远端固定构造轴的距离约束，仍需更广尺寸范围/布局验证。

`tests/acceptance/run_feature_probe.py` 已在真实 CATIA 执行：

- 主体 40×30×8，凸台 R6×4，顶部向下 R3 通孔：体积依次
  9600、10052.389342、9713.097336 mm³；最终包围盒 40×30×12 mm。
- L→50、T→10、R→4（依次修改同一新建模型）：体积分别
  12113.097336、15141.371669、15251.327412 mm³，均与解析值误差 <0.01 mm³。
- 从底部向外反向切除，实际无材料变化时返回 FEATURE_NO_EFFECT。
- 每次只关闭探针自己新建的文档，不保存或覆盖用户文档。

证据：`F:/CatiaAgent/work/feature-probe-1793172307810503200.json`。
相关离线测试共 **71 passed，0.39 秒**。该探针不等价于独立验收；尚未保存并
重开验证 CATPart，尚未验证圆孔中心坐标、单实体、Shaft/Groove、yz/zx、
相交轮廓及更广参数变体。MCP schema/路由、Agent 技能、预览仍待接入。

## 2026-10-28 续接记录：三方向保存与独立重开验收

独立验收增加 `fullCylinders`（完整圆柱面的半径、三维中心、轴向和高度），
以及按名称核对 `formulas`，保留旧板件/圆环验收入口。
圆柱面轴向比较先归一化；现场 GetAxis 返回过 `[10,0,0]`，不能假设单位长度。
此接口不适用于部分圆柱面，不能用其面积推算轴向高度。

验收进程现在明确检查 JSON 结构、非空布尔检查列表、退出码和每项检查；
子进程声称 PASS 但退出非零或含失败项时一律失败。增加 UTF-8 通信，
拒绝负数/非有限容差。

真实探针命令：`.venv/Scripts/python.exe tests/acceptance/run_feature_probe.py --plane xy`
（另以 yz、zx 各执行一次）。三个方向均保存独立 CATPart 并由独立 Python
进程重开；文件体积 15251.327412 mm³，参数 L=50、W=30、T=10、R=4。
圆孔/凸台半径 4/8 mm，高度 15/5 mm，中心及轴向按所在平面转换后核对。

证据报告（包含 CATPart 路径、哈希、独立检查和负例）：

- YZ：`F:/CatiaAgent/work/feature-probe-1793172604338105900.json`
- ZX：`F:/CatiaAgent/work/feature-probe-1793172615138630200.json`
- XY：`F:/CatiaAgent/work/feature-probe-1793172625925487700.json`

相关离线测试 **78 passed，0.42 秒**。仍未完成：任意组合的自动几何期望生成、
单实体检查、Shaft/Groove 现场验证、轮廓交叠检查、MCP/Agent 链路接入、
预览、实际 NIST 派生图及全量实验；不以三个合成案例替代这些要求。

## 2026-10-28 续接记录：从需求自动计算几何验收依据

新增 `catia_mcp/feature_geometry.py`，只在独立 OCCT 环境运行。
`cad_support` CLI 和私有 `run_support` 增加 `evaluate` 操作：输入
`{"requirements": {...}, "actual": "可选的生成件.step"}`。
它按需求计算 Pad/Pocket/Shaft/Groove，处理嵌套闭合轮廓的孔洞奇偶层级，
拒绝接触/相交轮廓、跨旋转轴负侧的轮廓、断开的多实体、无效实体及无效果特征。
输出每步体积和包围盒；提供 actual 时比较包围盒、缺失材料、额外材料。
这是对需求所定义几何的独立内核验证，不验证 Agent 对图纸的解释是否正确。

独立重开脚本增加可选 `exportStep`，拒绝覆盖已有输出。组合现场探针
增加 `--cad-python`，可将重开导出的生成件与需求计算几何比较：

```powershell
& .venv/Scripts/python.exe tests/acceptance/run_feature_probe.py --plane yz --cad-python F:/CatiaAgent/work/text-to-cad-env/Scripts/python.exe
```

实测报告：`F:/CatiaAgent/work/feature-probe-1793172881597368800.json`。
实际 STEP 单实体；包围盒误差 0 mm，缺失材料 4.91127e-11 mm³，额外材料 0 mm³，
默认长度/体积容差均为 0.01。期望几何不读取 NIST 参考模型，也不导入为 CATPart。

辅助环境测试 `test_feature_geometry.py test_cad_support_client.py test_cad_support.py`
共 **31 passed，18.47 秒**；主环境相关回归 **78 passed，0.35 秒**。
新测试含相同体积/包围盒但孔位错误的负例、三平面坐标、嵌套圆环、旋转/旋转切除，
以及多实体、轮廓交叠、无效果拒绝。旋转测试此处仅为 OCCT，CATIA Shaft/Groove
仍待现场验证。下一步将预检查、构建、独立重开和材料比对贯通到 MCP 与 Agent；
当前仍未注册新需求 schema，也未完成预览和 NIST 实验。

## 2026-10-28 续接记录：MCP 处理器主链路

新 schema 已接入现有 validate/compile/build，不新增参考模型读取工具。
公开 JSON schema 为 `drawing/feature_schema.py`，保留旧需求的 oneOf 分支。
`feature_workflow.py` 串联需求几何预检查、FeatureExecutor、保存、独立重开导出 STEP、
材料比较及逐参数副本修改验收。任一阶段失败返回 FEATURE_BUILD_FAILED，并携带
失败阶段/特征；保存成功但验收失败仍为 verification_failed。组合分支拒绝 expected
覆盖和 editCheck=false，不覆盖已有 CATPart/STEP/报告。

真实处理器探针：设置 CATIA_CAD_SUPPORT_PYTHON 后执行
`.venv/Scripts/python.exe tests/acceptance/run_feature_tools.py`。
结果 REQUIREMENTS_READY → PLAN_READY → BUILD_COMPLETED。
证据目录：`F:/CatiaAgent/work/feature-tools-1793173179559830000/`，其中
`composition.CATPart` 为新建交付，`composition.build.json` 包含完整追溯和材料比对。
四参数分别在副本改值：L 40→40.4、W 30→30.3、T 8→8.08、R 3→3.03，
均通过独立重开测量、修改后几何匹配以及“相对原几何确有变化”的检查。
离线相关回归 **81 passed，0.48 秒**。

Agent 技能已加入组合需求写法、坐标/偏移/方向、真实证据引用及错误处理约定；
插件源配置加入 CATIA_CAD_SUPPORT_PYTHON 和 CATIA_OCR_PYTHON。
尚未重新安装插件或重启正在运行的服务；此证据属于真实工具处理器，不是浏览器
上传/模型自主规划验收。预览、旋转现场专项、NIST 派生图和全量验收仍未完成。

## 2026-10-28 续接记录：三维预览

私有辅助 CLI 增加 `preview`：由生成件 STEP 三角化输出
`catia-preview-mesh/1` JSON（mm、顶点、三角形、源 STEP 哈希），拒绝覆盖、
无效几何、超过 10 万三角形或 15 MB 的网格。组合工作流在验收后生成
`.preview.json`，将路径/哈希写入 delivery.preview；预览失败单独报告，
不改变工程验收结果。旧板件/圆环路径暂未自动生成此产物。

前端结果卡新增查看/收起入口，复用受项目边界保护的文件内容接口；
原生 WebGL 深度渲染，无新增依赖，支持拖动/方向键旋转、滚轮缩放、重置。
前端校验网格格式、尺寸、索引、数量及哈希；预览不替代 CATPart 验收。

验证：前端预览/结果卡 9 项通过；真实 OCCT 网格生成及禁止覆盖测试通过。
ego-browser TaskSpace 1 中，以临时烟雾测试页面加载真实生成件网格（528 个三角形），
确认主体/凸台/通孔可见且方向键改变视角。截图：
`F:/CatiaAgent/work/preview-browser/preview.png`、`rotated.png`。
临时页面和公共网格测试文件已移除；该检查模拟文件响应，不替代实际聊天项目授权链路。

全量前端 tsc 未通过，报错在未修改文件：assemble.test.tsx:10、runtime.ts:129、
useSessionStore.streaming.test.ts:27；本次新增模块未报告类型错误。
仍需实际工具构建产物→聊天卡片→项目文件服务的端到端预览验证、旧路径接入，
以及此前剩余的旋转、NIST 和用户上传验收。

## 2026-10-28 续接记录：旋转、旧流程预览及上传实测

真实 Shaft + Groove 处理器探针已通过，证据目录：
`F:/CatiaAgent/work/feature-tools-1793173952612639900/`。
基线体积 1963.495408 mm³；STEP 包围盒误差 0 mm，缺失材料约
1.11e-11 mm³，额外材料 0 mm³。独立副本修改 R 5→5.05、T 10→10.1、
A 360→356.4 均通过重开及精确几何比较；预览 768 个三角形。
曲面 STL 极值存在约 0.018791 mm 离散误差，组合分支的精确 bbox 检查改由
强制 STEP 比较承担，容差保持 0.01 mm；COM 重开仍检查 bbox 有效性。
失败反例确认 STEP 不匹配不能因 COM 重开 PASS 而完成交付。
相关主环境回归 **82 passed**。

`drawing/preview.py` 复用独立重开导出和预览辅助进程，旧板件/圆环/轮廓路径
亦已接入。DP-01 真实处理器回归 BUILD_COMPLETED / PASS，预览 1052 个三角形，
证据：`F:/CatiaAgent/work/legacy-preview-20261028-1600/acceptance-report.json`。
Agent 默认交付位置改为当前工作区 deliveries 子目录，供项目文件服务读取；
旧执行器保存时创建父目录，避免完成建模后才因目录不存在失败。

前端原有三处类型阻塞已作最小修正：assemble 测试使用带 id/component 的
chatSurface、runtime 对 selection 可空判定、streaming 测试补 transcriptDiagnostics。
完整 tsc 通过；相关四组前端测试 **81 passed**。

插件安装前已备份到 `work/catia-plugin-backup-20261028-1553`，安装至当前 PILOT_HOME。
通过 WebSocket 查询 13 个历史会话，均 active=false；调用网关 reloadExtensions 成功，
没有为加载插件重启整个 dev 栈。

真实浏览器上传 DP-01 已执行，会话 `web-s_48c2cd5b-b7cc-4723-bff3-40ee36f9d2ed`，
工作区 `work/text-to-cad-ui-20261028`。Agent 自行读图、写需求、编译并保存 CATPart，
但网关在独立验收中退出（3221226505），本轮记为 **interrupted，不能算端到端通过**。
已确认没有仍在运行的建模/验收子进程后，通过 UI 重试启动网关。
保存件事后独立重开 **13 项 PASS**：`work/interrupted-ui-verification.json`；
这不补足被中断的参数修改/报告/聊天预览环节，也不改变本轮失败结论。

该上传同时暴露 OCR 内存分配失败：整页 float32 张量单次约 293 MiB，系统当时
提交内存余量仅约 2 GiB。`ocr_image.py` 改为 896 像素、128 重叠的原分辨率分块，
另用全页缩略图补读长标注；竖向读数还原到页坐标，内部边界截断文字丢弃。
渲染 pixmap 在启动 OCR 子进程前释放。实际 A3 合成图识别出
40/30/20/15/12/8/6/4，仍会有重复/误识别，必须核对图面。
制图测试改走生产同样的子进程边界，使渲染进程退出后再做 OCR。
辅助几何、制图、OCR/证据回归 **42 passed，35.09 秒**（内存恢复后的实测）。

新增合成组合验收输入 `work/composition-drawing/bracket.pdf`，含三个正交视图及轴测图，
尺寸齐全，已渲染检查；来源、规格及哈希见旁边 provenance.json。
它不是 NIST 案例。浏览器原 TaskSpace 1 已不存在，已请求用户允许重建；
完整组合上传及实际聊天文件服务预览仍待验收。

NIST 历史基线仍为 FTC-11 两次原始 PDF 端到端通过（见既有批次报告），
不是本轮新增结果。其余 16 例的充分派生图、必要特征与全量运行仍未完成；
本轮合成测试不计入 NIST 完成数。

## 2026-10-28 续接记录：参数化多边形

组合 profiles 新增 polygon，3–128 个有序顶点，自动闭合；每个 x/y 为带证据的
参数表达式或零。编译器拒绝零边、回折、自交、自接触和零面积；保持严格字段检查。
CATIA 中用逐顶点横纵定位约束与相邻端点重合约束驱动，支持负坐标及 xy/yz/zx。
独立 OCCT 按同一需求建立多边形面并执行加料/切除；执行前重新编译计划，拒绝被篡改的坐标。

真实验收命令：
`.venv/Scripts/python.exe tests/acceptance/run_feature_tools.py --polygon --cad-python F:/CatiaAgent/work/text-to-cad-env/Scripts/python.exe`。
证据 `work/feature-tools-1793176619132503500/`：YZ 平面负坐标凹板 + 三角盲槽，
返回 BUILD_COMPLETED，保存件 96659 字节；体积理论值 7050 mm³，包围盒
X 0–8、Y -20–20、Z -15–15 mm；完整 STEP 比较及 L/W/T 逐参数副本验收通过。
几何组 10 项通过。多边形不含圆弧，不用折线近似替代 NIST 中实际的圆角/圆弧。

追加的 OCR 坐标断言复验遇到机器提交内存耗尽：最小一次 PNG 保存 11 MiB 也分配失败。
此前 42 项完整辅助回归通过的事实保持，但新坐标断言尚待资源恢复后通过。
没有终止机器上不属于本任务的 Python 推理进程。

随后资源余量恢复至约 4 GiB，单独复跑新增 OCR 坐标断言通过（1 passed，8.73 秒）。
最终主环境相关回归 **90 passed**。多边形实测体积 7050 mm³，精确 bbox 误差、
缺失材料、额外材料均为 0；L 40→40.4、W 30→30.3、T 8→8.08 修改均通过。
预览 32 个三角形。更新后的 Agent 技能已同步到当前 PILOT_HOME。

## 2026-10-28 续接记录：参数化直线/圆弧混合轮廓

组合 profiles 新增 wire：一个起点和 2–128 条有序 line/arc 段，最后终点回到起点。
圆弧由端点、参数化半径、clockwise 与 largeArc 确定；默认逆时针小弧。
编译检查闭合、非零边、弦长/半径关系及严格字段；OCCT 预检拒绝无效或自交轮廓。
CATIA 使用原生圆弧、半径与端点坐标公式、相邻端点重合约束，未使用折线近似。
工具描述、JSON schema、Agent 技能和现场探针均已同步到源码；新技能尚未重新安装。
同时将技能中的实际 FTC-11/DP-01 示例替换为虚构例子，避免示例泄露案例答案。

以下真实 MCP 处理器探针均返回 BUILD_COMPLETED，保存 CATPart 后由独立进程重开、导出 STEP
进行精确几何比较，并逐参数修改副本通过验证；它们是合成几何测试，不是 NIST 结果：

| 轮廓 | 实测体积 mm³ | 证据目录（F:/CatiaAgent/ 下） |
| --- | ---: | --- |
| 长圆槽切除 | 8413.805329 | work/feature-tools-1793177307812973900 |
| 顺时针大圆弧 | 235.619449 | work/feature-tools-1793177550761878600 |
| 顺时针小圆弧 | 21.460184 | work/feature-tools-1793178024184948900 |
| 逆时针大圆弧 | 335.619449 | work/feature-tools-1793178077381992100 |
| 逆时针小圆弧 | 78.539816 | work/feature-tools-1793178145864300300 |

长圆槽验证 L/W/T/R，四种圆弧验证 R/T。每份目录含 composition.CATPart、build.json、STEP
和 preview.json。OCCT 几何组 **15 passed**；新增隔离前主环境相关回归 **97 passed**。

## 2026-10-28 续接记录：生成实验的文件与工具边界

新增可选 `CATIA_GENERATION_ROOT` 模式，只有 inspect/crop/validate/compile/build 五个工具可见且可调用。
服务在 COM/文件操作前解析并限制输入 PDF、证据、输出及交付路径；拒绝目录外路径、相对路径、
解析后逃逸的链接和替代数据流。证据、自动期望值及参数修改验收不能被关闭或覆盖。
crop 返回内嵌 MCP PNG，使生成会话无须开放 read_file。未设置该变量的服务保持既有范围。

AgentCPM 准备脚本为每次实验建立新的 PDF-only workspace，主机配置/清单在兄弟 host 目录，
会话配置只允许专用 MCP 的五个工具，系统提示词取通用技能，禁用其他技能。
脚本不接收参考 CAD 或预期尺寸。说明见
`AgentCPM/products/catia-agent/acceptance/pdf-generation-isolation.md`。
这不是 OS 沙箱；必须和宿主工具白名单一起使用，不应把单独设置环境变量当成完全隔离。

审查发现宿主原有 MCP Markdown 图片链接回退会读取 cwd 外路径；已限制规范化/解析后的实际路径
必须在该服务 cwd 内，并覆盖目录穿越及 junction 逃逸测试。完整 TypeScript 检查通过。
**运行中的 Gateway 尚未重启加载此宿主修复，新隔离模式尚未经过真实 Agent 生成验收。**
准备脚本状态明确为 prepared_not_executed，不能算已执行实验。

验证：主环境相关回归 **123 passed（1.36 秒）**，Node 范围与准备脚本 **6 passed**；
真实 stdio 探针验证精确五工具清单、参考 verify 拒绝、目录外读写拒绝、真实 PDF 读图及内嵌裁图，
报告 `work/generation-scope-1793178751054182100/report.json`，6 项全部通过。
该探针未建模，不替代前面的真实 CATIA 构建，也不替代用户浏览器上传验收。

NIST 仍为历史 FTC-11 两次通过（1/17）；其余案例的完整图纸、必要特征和全量实验仍待完成。
浏览器 TaskSpace 恢复与 MinerU 本地不可用后的解析路线仍待先前问题的用户选择；
没有擅自新建替代浏览器空间或把 NIST 文档上传远程解析。

## 2026-10-28 暂停前收尾：真实隔离 Agent 链路与倒角修复

用户要求先收尾，开发目标已暂停；以下覆盖上文“Gateway 尚未加载、隔离 Agent 尚未验收”的旧状态。
确认 14 个已登记会话无活动任务且无 COM 构建/验收进程后，经现有监督进程仅重启 Gateway，
新 PID 69412；未重启 UI/Vite、未清空历史。最新 PDF 技能已备份并同步至插件及独立技能目录，
备份 `work/catia-skill-backup-20261028-1729/`；reloadExtensions 成功。

真实 Gateway 合成组合件运行：
`work/isolated-generation/e21740ad-5878-4fe1-b247-9fb9e5b2e7d0/`，
17:25:45–17:32:48，10 次调用，只使用专用五工具；包含一次相对裁图输出路径被拒绝后自行改正。
Agent 独立读取 OCR 与图片并选择矩形板 Pad、圆凸台 Pad、顶部反向通孔 Pocket。
最终 BUILD_COMPLETED，CATPart 97037 字节，体积 9713.09733552923 mm³，bbox 为 [0,0,0]–[40,30,12]。
L/W/T/BOSS_D/BOSS_H/HOLE_D/POS_X/POS_Y 共 8 参数副本改值验证全部通过，预览 528 三角形。
host/manifest.json 记录 generated_verified；完整模型调用在 events.jsonl；正式交付及追溯在
generation/deliveries/part.CATPart 与 part.build.json。

生成侧不可见的原始合成模型独立比较 PASS，bbox/缺失材料/额外材料误差均为 0；报告
host/hidden-comparison.json。比较输入与结果保留在 host，未回填生成会话。
此结果是合成案例的真实 CLI Agent 验收，不替代浏览器上传/聊天预览或任何 NIST 案例。
三个前置失败（空技能列表、SDK 新插件技能未注册、项目未登记）保留原目录；准备及提交脚本已修正。

为后续 NIST 边缘特征做了原生 API 探针：新建方块，重新枚举最终实体边，按中点 [20,0,8]
唯一匹配后立即消耗 Reference；原生 fillet 与 chamfer 的尺寸公式修改均产生正确体积。
探针 `tests/acceptance/run_edge_finish_probe.py`，证据分别为
`work/edge-finish-fillet-1793180037967340000/`、`work/edge-finish-chamfer-1793180173868876900/`。
这些只是 API/参数传播探针，尚未接入组合特征语言，也未完成独立重开与精确材料比对，不作为完整新能力。

过程中复现既有 catia_chamfer 的模式错误：mode=0 实际是双长度，会把 angle=45 当成 45 mm。
从本机 PartTypeLib.tlb 核对 mode=1 才是 length-angle，已修正并补失败先行回归。
真实公开工具探针：2 mm×45° 的体积 9520 mm³；尺寸改 2.2 后 9503.2 mm³，均符合解析值。
类型库记录 `work/part-typelib-edge-api.json`；首次失败目录保留。

收尾复验：主环境 **131 passed（1.53 秒）**；Node **6 passed**。
之前本轮 OCCT 几何组 15 passed、完整 TypeScript 检查通过。最后检查无运行中的生成/建模/独立验收工作进程。
未提交或清理两仓库既有修改。普通常驻服务保持运行；最新倒角源码需新 MCP 进程加载，未额外重启服务。
待继续：完整浏览器链路、实际 NIST 派生图纸审查、边缘/其他缺失特征接入和剩余 16 例全量实验。
