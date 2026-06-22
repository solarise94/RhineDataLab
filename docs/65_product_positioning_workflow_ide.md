# 65. 产品定位决策：工作流 IDE（不是自动科研平台）

Status: positioning decision (supersedes the autopilot framing in `docs/00`).

Date: 2026-06-21

Related:

- `docs/00_overview_blueprint.md`（被本文档修订的 autopilot 倾向表述）
- `docs/06_morphmind_specialist_card_ui_brief.md`（已有的“工作台而非 workflow
  editor”方向，与本文档一致）
- `docs/18_manager_auto_mode_wake_hook_plan.md`（autopilot = RUN ALL 的运行模式）
- `docs/64_docker_and_macos_packaging_plan.md`（受定位影响的打包方案）

## 1. 为什么要单独写这份文档

代码、README、`docs/00` 三者之间存在定位张力，历史文档把产品推向“全自动科研平台”
方向，但实际代码实现和 README 表述的是另一回事。这种不一致会让后续所有架构决策
（打包、沙箱、执行模型、Manager 边界）失去共同锚点。本文档把定位一次性钉死，
后续文档和代码以此为准。

## 2. 定位陈述

> **RhineDataLab 是一个工作流 IDE。用户用它编辑由“执行器节点”组成的工作流
> （卡片蓝图 / graph），然后运行工作流。Manager AI 是帮助用户编辑工作流的智能
> 助手，不是替用户运行分析的自动驾驶。**

用三个类比锚定，避免再次漂移：

| 类比对象 | 对应关系 | 说明 |
|----------|----------|------|
| RStudio / RStudio Server | 整体产品形态 | 本地工作台 + 可选远程访问；交互式、单步可重跑 |
| Dify / 工作流编排器 | 工作流编辑模型 | 用户编辑节点图（卡片）、连依赖、设参数，然后运行 |
| 传统 LLM 直接写代码 | Manager 的角色对照 | 传统 LLM 写“代码”；这里 Manager 写“工作流”，工作流子模块是执行器 |

一句话：**传统 LLM 把意图翻译成代码；RhineDataLab 的 Manager 把意图翻译成工作流
（卡片图），工作流的节点是执行器。**

## 3. 三个关键概念的精确边界

### 3.1 Manager = 编辑器智能助手（不是 autopilot）

Manager 的核心职责是**编辑工作流**：

- 用户提需求 → Manager 产出/修改卡片图（patch）→ 用户审阅 → 落盘
- Manager 帮用户“写工作流”，类比 IDE 里 Copilot 帮用户“写代码”
- Manager **不替用户运行**，运行是用户显式触发（单卡片）或 autopilot 模式触发（全图）

`docs/00` 中“用户不直接编辑 Graph IR / YAML / JSON 蓝图”“用户权限停留在 Intent
Level”这类表述是**过度自治化**的，与实际代码（`runs.py` 提供 `start-run` /
`rerun` / `reset-run-state` 的单卡片级手动操作）、README（“重复单步计算”“新开分支”）
和 `docs/06`（“工作台而非 raw graph editor”）都不符。本文档将其修订为：

> 用户通过 Manager 编辑工作流，也可以直接操作卡片（手动跑、重跑、重置、连线）。
> Manager 是编辑助手，降低手工编辑成本，不垄断编辑权。

### 3.2 autopilot = RUN ALL（不是自治 agent）

`manager-auto` / wake hook 机制的本质是 **RStudio 里 “Run All Chunks” 的等价物**：

- 用户已编辑好工作流（卡片图）
- 用户显式开启 `/auto`
- 系统顺着已有工作流，把 ready 的卡片依次跑完
- 遇到依赖缺失等 routine blocker，harness 自动处理（装依赖、唤醒），不把每个小
  决策都甩给用户
- **autopilot 不会自动新增分析模块**——它只“跟着蓝图跑完”，不“扩展蓝图”

这是**运行模式**，不是**编辑模式**。`docs/18` 的实际设计（“继续运行 ready cards
然后休眠”“不绕过 WorkerService/Reviewer/manifest validation”）已经符合这个定位；
只是 `docs/00` 的措辞把它抬成了产品主线。本文档将其降回为“可选的批量运行模式”。

### 3.3 执行器 = 工作流节点

每个 card 背后是一个 executor（`pi` / `claude_code` / `codex` / `opencode` /
`shell`）。用户编辑的是“这些节点怎么连、参数是什么”，运行时节点各自执行。

这与 Dify 的模型一致——只是 Dify 的节点是“HTTP 调用 / 条件分支”，这里的节点是
“LLM coding agent 跑一段分析”。代码已支撑此模型：

- 运行粒度是 `card_id`（`POST /cards/{card_id}/start-run`），等价单节点调试
- `modules.json` / `cards.json` / `assets.json` / `runs.json` 分离存储，是节点图 + 运行记录结构
- executor 之间通过 asset（资产）传递依赖，asset 有 `depends_on` 形成数据流 DAG

## 4. 这个定位如何约束技术架构

定位确定后，以下架构决策随之收敛（具体设计见 `docs/64` 打包、`docs/66` 执行模型）：

| 维度 | 定位带来的约束 |
|------|----------------|
| Desktop vs Server | IDE 是交互式，桌面是一等产物；Server 是“远程访问同一工作台”（RStudio Server 定位），不是“后台自治平台” |
| 沙箱威胁模型 | 隔离的是“执行器节点里 LLM 生成的代码”对 FS/进程的越权，不是隔离用户；交互式场景要求沙箱启动开销小（秒级响应），故保留轻量沙箱（bwrap/seatbelt/container），不上 OCI 重型隔离 |
| manager-auto | 保留为可选运行模式，不作为核心；默认姿态是手动（用户点 card 跑），autopilot 显式开启 |
| 仓库结构 | 一个仓库、三个构建产物（Linux 包 / Docker / macOS app），代码层面无 server-only / desktop-only 分叉点 |
| 执行粒度 | 单卡片跑（单节点调试）+ 全图跑（RUN ALL）是已有两种模式；选区运行（跑子图）是待补的第三种（见 `docs/66`） |

## 5. docs/00 需要修订的具体表述

以下 `docs/00` 的措辞与本定位冲突，后续整理文档债时修订（本文档先记录修订意图，
不立即改 `docs/00`，避免与正在进行的其他文档工作交叉）：

1. `docs/00 §1`：“前台像一个简单的 specialist / manager AI 对话产品；后台像一个
   严谨的 Git-native 版本化生信分析项目系统”
   → 修订为“工作流 IDE：前台是 Manager 对话 + 卡片工作台，后台是 Git-native
   版本化的工作流项目系统”。

2. `docs/00 §2.1`：“用户只编辑意图，不直接编辑蓝图”“用户权限停留在 Intent Level”
   → 修订为“用户通过 Manager 编辑工作流，也可直接操作卡片；Manager 是编辑助手，
   降低手工编辑成本，不垄断编辑权”。

3. `docs/00 §1` “用户主要通过 Manager AI 提需求… 用户不直接编辑 Graph IR”
   → 保留“Manager 辅助编辑”，删除“用户不直接编辑”的绝对化表述。

4. README 的定位表述（“本地工作台”“重复单步计算”“新开分支”）与本定位一致，
   不动。

## 6. 不变的底线

无论定位措辞怎么调，以下产品底线不变（来自 `docs/00`、README，代码已实现）：

- Git-native 版本化：项目状态、卡片图、运行历史、资产都落盘且可追溯
- Worker 自由、Graph 严格：执行器可自由读项目文件，但图更新必须过 manifest 校验
- Reviewer 校验：跑完不等于结束，结果要过 review 才 promote 为 valid
- 本地优先：默认本地运行，远程/服务器是可选访问形态

## 7. 结论

本文档是后续 `docs/64`（打包）、`docs/66`（执行模型与依赖链）的定位锚。两份文档
的设计决策若与本文档冲突，以本文档为准。
