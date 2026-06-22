# 66. 执行模型与依赖链重设计：选区运行 + 重跑失效传播

Status: §3 + §4 form 1 implemented (first increment); forms 2/3 and restart recovery remain future work.

Date: 2026-06-21

Related:

- `docs/65_product_positioning_workflow_ide.md`（定位锚：工作流 IDE）
- `docs/18_manager_auto_mode_wake_hook_plan.md`（autopilot = RUN ALL）
- `backend/app/services/worker_service.py`（`rerun_card` / `start_run`）
- `backend/app/services/asset_materialization_service.py`（资产 supersede）
- `backend/app/services/dependency_attention_service.py`（下游失效检测）

## TL;DR

两个纠缠的问题放一份文档，因为它们共享同一个根因——**资产失效是“被动扫描检测”
而非“事件驱动传播”**：

1. **重跑导致依赖链断裂**：重跑一个卡片 → 旧输出资产被标记 `superseded` → 下游
   卡片状态**不会自动变 `stale`**，要靠 `DependencyAttentionService` 全图扫描才发现。
   用户重跑上游后不知道哪些下游需要重跑，依赖检查按需触发而非自动级联——这是
   “焦头烂额”的根源。

2. **缺选区运行**：只有“单卡片跑”（单节点调试）和“全图跑”（RUN ALL），缺“跑子图”
   （从某卡片跑到某卡片 / 跑选中的一组卡片）。用户改了中间步骤，想局部重跑而不过
   全图，目前做不到。

本文档先讲清根因（§2），再给重新设计（§3 选区运行 + §4 事件驱动失效传播）。

---

## 1. 现状执行模型回顾

### 1.1 两种已有运行模式

| 模式 | 入口 | 粒度 | 触发方式 |
|------|------|------|----------|
| 单卡片运行 | `POST /cards/{card_id}/start-run` / `rerun` / `reset-run-state` | 单个 card | 用户手动点 |
| 全图运行（RUN ALL） | `manager-auto` / wake hook | ready 卡片依次跑完 | 用户显式 `/auto` |

`rerun_card`（`worker_service.py:697`）的逻辑：把卡片状态重置为 `planned` → 清理旧
run 文件 → 调 `start_run`。重置时只动了**这一张卡片**，没碰下游。

### 1.2 资产与依赖的数据模型

```
Card
 ├─ inputs:  [{asset_id, ...}]      # 依赖的上游资产
 ├─ outputs: [{asset_id, ...}]      # 产出的资产
 └─ status: planned/running/reviewing/accepted/failed/stale/superseded/...

Asset
 ├─ asset_id, status, path
 ├─ depends_on: [asset_id, ...]     # 这个资产由哪些资产派生
 └─ created_by_run: run_id

graph.metadata["asset_materializations"]
 └─ planned_asset_id -> {current_asset_id, superseded_asset_ids[], ...}
     # 逻辑资产 → 当前具体资产的绑定；重跑时旧资产进 superseded_asset_ids
```

依赖链是通过 **asset 的 producer-consumer 关系**隐式形成的：卡片 A 产出 asset X，
卡片 B 的 input 引用 X，则 B 依赖 A。`DependencyAttentionService.affected_downstream`
（`dependency_attention_service.py:103`）用 BFS 沿这个关系计算下游受影响卡片。

### 1.3 重跑时发生什么（现状）

以“卡片 A →（产出 asset X）→ 卡片 B（消费 X）→ 卡片 C（消费 B 的产出）”为例，
用户重跑 A：

1. `rerun_card(A)`：A 状态置 `planned`，旧 run 文件清理。
2. A 重新执行成功，`_finalize_run_review` accept 时：
   - 产出新 asset X'（`asset_materialization_service.set_current`）
   - 旧 asset X 被记入 `superseded_asset_ids`，X 的 `status` 在 graph copy 上被置
     `superseded`（`worker_service.py` `_finalize_run_review` 内）
3. **此时 B 和 C 的状态没有任何变化**——它们还指向旧的 `accepted`，但实际上 B 的
   input（X）已经失效了。
4. 只有当某处调用 `DependencyAttentionService.analyze_project`（全图扫描）时，才会
   发现 B 的 input asset 是 `superseded`，报一个 dependency attention issue。
5. 用户/Manager 看到这个 issue 后，才手动 rerun B；B 跑完又 supersede 它的产出，
   C 又要等下一轮扫描……

**这就是断裂**：失效传播是**拉模式（pull / 按需扫描）**，不是**推模式（push / 事件驱动）**。
重跑上游后，下游不会自动变 `stale`，用户得不到“哪些卡片需要重跑”的即时反馈。

---

## 2. 根因分析

### 2.1 直接根因：supersede 是写时动作，stale 标记是读时检测

`AssetMaterializationService.set_current`（`asset_materialization_service.py:39`）在
绑定新资产时把旧资产移入 `superseded_asset_ids`——这是**写时**完成的。但“下游卡片
因输入失效而应变 stale”这件事，没有任何代码在 supersede 发生时同步执行。

`DependencyAttentionService` 是**读时检测**：它扫全图，对每张卡片检查 input asset
状态是否在 `VALID_INPUT_STATUSES = {"valid", "candidate"}`，不在就报 issue。这是个
诊断工具，不是状态机推进器——它发现 issue 但**不修改卡片状态**。

所以重跑后存在一个“状态真空期”：资产已 supersede，卡片状态还停在 `accepted`，直到
有人主动查依赖才会发现问题。

### 2.2 次要因素：rerun 不带失效传播语义

`rerun_card` 当前的语义是“重置这一张卡片并重跑”，它不知道也不关心下游。从单卡片
调试角度看这没错（用户可能只想重跑这一张看输出），但从工作流 IDE 角度看，用户重跑
上游通常的意图是“从这里开始重新生成结果”，下游理应失效。

现状把“是否传播失效”这个决策**完全丢给用户**——用户得自己判断哪些下游要重跑，
逐个手动 rerun。卡片一多就不可维护。

### 2.3 为什么选区运行也卡在这里

选区运行（跑子图）的本质是“确定一个卡片集合 + 按拓扑序执行”。要确定“哪些卡片属于
这个子图”，必须能算依赖闭包——而这正是 `affected_downstream` 已经能做的事。但
`affected_downstream` 只算“下游”，选区运行还需要“上游 + 自身 + 下游”或“两点之间的
路径”，现状没有这个能力。而且即使算出子图，执行时也要处理“子图内卡片重跑会不会
再次 supersede、再次需要传播”——和 §2.1 是同一个问题。

所以**选区运行和重跑失效传播必须一起设计**：失效传播解决了，选区运行才能正确地
“跑一个子图并让子图内外的状态都自洽”。

---

## 3. 设计：事件驱动的失效传播

### 3.1 核心思路：supersede 发生时，主动把下游标记 stale

把失效传播从 pull 改成 push。在 `AssetMaterializationService.set_current`（或其调用
方 `_finalize_run_review`）完成旧资产 supersede 后，立即触发一次**下游 stale 标记**：

```
重跑卡片 A，accept 后：
  1. 旧 asset X → superseded（现有）
  2. [新增] 触发 DownstreamInvalidationService.invalidate_from(A)
     - 用 affected_downstream 算法算出 B, C（BFS 沿 producer-consumer）
     - 对每张下游卡片：
       * 若状态为 accepted/needs_review → 置 stale，记录 stale_reason="upstream X superseded by rerun of A"
       * 若状态为 running/reviewing → 不动（运行中不打断，运行完自然走校验）
       * 若已是 stale/failed/planned → 不动（已经是待跑态）
     - 下游卡片的 accepted 产出资产 → 置 stale（级联，因为它们的数据源已失效）
  3. 发事件通知前端刷新（卡片状态变了）
```

这样重跑 A 后，B、C 立刻变 `stale`，用户一眼看到哪些要重跑。

### 3.2 stale 语义的精确化

现状 `stale` 是卡片状态之一，但语义模糊（“过期了”）。重新定义为：

> **stale = 该卡片曾成功跑过，但其输入资产已被上游变更失效，需要重跑才能重新 valid。**

`stale` 卡片：
- 保留上一次的产出资产（不删，标记为 stale），用户可对比新旧
- 可被 `rerun`（从 stale 态重跑，等价 docs/66 §3.3）
- 在 autopilot RUN ALL 中视为 ready（自动重跑）
- 前端用醒目样式标识，提示“上游已变更”

资产 `stale` 状态语义同步收紧：资产 stale 当且仅当其 producer 卡片 stale，或其
`depends_on` 中有 stale/superseded 资产。

### 3.3 级联深度控制（避免过度传播）

全图级联标记 stale 可能波及很广（改一个上游，整条链都 stale）。需要控制：

- **默认全级联**：重跑 A，所有下游（B、C、D……）都标 stale。这是正确语义——数据流
  失效是传递的。
- **可选范围限制**：rerun 时带 `propagate: "all" | "none" | "depth:N"` 参数：
  - `all`（默认）：全下游级联
  - `none`：只重跑这一张，不动下游（等价现状的单卡片调试语义）
  - `depth:N`：只标 N 层下游 stale（少见，留口子）

`none` 保留了“我就想单独重跑这张看输出”的调试场景，不强制传播。

### 3.4 与 autopilot 的配合

失效传播变 push 后，autopilot RUN ALL 自然受益：重跑上游 → 下游自动 stale →
autopilot 把 stale 视为 ready → 自动重跑下游。整条链自动推进，不需要 `DependencyAttentionService`
扫描驱动。`DependencyAttentionService` 降级为**诊断/审计工具**（用户主动查“现在图里
有哪些依赖问题”），不再承担状态推进职责。

### 3.5 循环依赖保护

工作流应是 DAG，但编辑过程可能临时引入环。失效传播的 BFS（复用 `affected_downstream`
的 `seen` 集合）天然防环——已访问的节点不重复入队。若检测到环（某下游又指回上游），
标记 issue 但不死循环。

---

## 4. 设计：选区运行（跑子图）

### 4.1 三种选区运行形态

| 形态 | 含义 | 用例 |
|------|------|------|
| **从某卡片跑到末尾** | 指定起点卡片，跑它 + 它的所有下游（拓扑序） | 改了中间步骤，想从这里往后全部重生成 |
| **跑选中的一组卡片** | 用户多选若干卡片，按拓扑序跑这组 | 只想重跑某几个特定步骤 |
| **跑两点之间的路径** | 指定起点 + 终点，跑起终之间的依赖闭包 | 重跑某段管道 |

形态一是最常用的（对应“改了中间，往后重跑”），优先实现。形态二、三在其基础上扩展。

### 4.2 API 设计

新增 `POST /projects/{project_id}/runs/subgraph`：

```jsonc
// 请求
{
  "mode": "from_card",          // "from_card" | "selected" | "between"
  "start_card_id": "card_A",    // from_card / between 的起点
  "end_card_id": "card_C",      // between 的终点
  "card_ids": ["card_A","card_B"], // selected 的显式集合
  "propagate": "all",           // 失效传播范围，同 §3.3
  "python_runtime": "...", "r_runtime": "..."  // 运行时绑定
}

// 响应：一个 batch run
{
  "batch_run_id": "batch_...",
  "planned_cards": ["card_A","card_B","card_C"],  // 拓扑序
  "status": "queued"
}
```

### 4.3 子图计算

`from_card` 模式：起点卡片 + `affected_downstream(起点)` = 子图闭包。
`selected` 模式：显式集合（可选地补全集合内卡片间的依赖闭包，保证拓扑序完整）。
`between` 模式：起点 + 终点之间所有路径上的卡片（起点到终点的支配闭包）。

拓扑序由依赖关系决定：若 B 依赖 A 的产出，则 A 先跑。复用现有 `DependencyAttentionService`
的 producer-consumer 索引计算拓扑序。

### 4.4 子图执行的并发与依赖

子图内卡片按拓扑序执行，同层（无互相依赖）可并发（受 `BLUEPRINT_EXECUTOR_MAX_CONCURRENT_RUNS`
信号量限制，现有机制）。一张失败的处理策略：

- **默认 stop-on-fail**：子图内某卡片失败，暂停后续，已完成的保留，未跑的置 `planned`。
  用户修复后可“续跑”（跳过已 success 的，从失败处继续）。
- **可选 continue-on-fail**：失败的不阻断后续无依赖关系的卡片继续跑。

### 4.5 子图运行与失效传播的关系

选区运行**不替代**失效传播，而是**建立在失效传播之上**：

- 选区运行前，先按 §3 把起点卡片的下游标记 stale（若 `propagate=all`）
- 子图内的卡片从 stale/planned 态开始跑
- 子图外的下游（若有，`between` 模式终点之后的）也按 `propagate` 标 stale

这样保证“跑完一个子图后，整个图的状态自洽”——子图内是 fresh 的，子图外该 stale
的都标了 stale，不会出现“跑了一半、状态对不上”的真空。

### 4.6 与单卡片运行、RUN ALL 的统一

三种运行模式统一到“执行计划”概念下：

| 模式 | 执行计划 |
|------|----------|
| 单卡片运行 | 计划 = [该卡片] |
| 选区运行 | 计划 = 子图闭包（拓扑序） |
| RUN ALL | 计划 = 全图所有 ready/stale 卡片（拓扑序） |

底层都走 `WorkerService` 的 run 调度，区别只在“计划里有哪些卡片”和“失效传播范围”。
单卡片运行 = `propagate=none` 的选区运行特例；RUN ALL = `from_card=图根` 的选区运行
特例。这样执行模型收敛，不为选区运行新造一套调度。

---

## 5. 实现影响评估（供后续实现参考）

### 5.1 新增/修改

| 组件 | 改动 |
|------|------|
| 新增 `DownstreamInvalidationService` | 封装“从某卡片起，把下游卡片+资产标 stale”的逻辑，复用 `affected_downstream` 的图遍历 |
| `AssetMaterializationService.set_current` 调用方 | supersede 旧资产后，调 `DownstreamInvalidationService.invalidate_from(producer_card)` |
| `worker_service.rerun_card` | 增加 `propagate` 参数，默认 `all`，调失效传播 |
| 新增 `runs` API `POST /runs/subgraph` | 选区运行入口，算子图 + 生成 batch run |
| `WorkerService` | 支持 batch run 调度（拓扑序 + 并发 + stop/continue-on-fail），单卡片运行和 RUN ALL 复用 |
| 前端 | 卡片 stale 态展示；选区运行 UI（多选 + “从这里跑到末尾”）；batch run 进度 |

### 5.2 不改的

- 资产数据模型（`Asset`、`asset_materializations`）不变，只新增 stale 的主动写入
- `DependencyAttentionService` 不删，降级为诊断工具
- manifest 校验、reviewer 流程不变
- executor / sandbox 不变（这是执行层，和依赖链层正交）

### 5.3 风险点

1. **全级联 stale 的用户感知**：改一个上游，可能十几张卡片全 stale，视觉冲击大。
   缓解：前端把 stale 做成“可一键批量重跑”的分组，而不是一排红字。
2. **stale 资产的清理时机**：stale 资产保留多久？建议保留到该卡片下次成功 run 后
   supersede，不主动删（用户可能要对比新旧结果）。
3. **batch run 的中断恢复**：进程重启后 batch run 状态恢复，需持久化 batch_run_id
   及其 planned_cards 到 `graph/runs.json` 或独立文件。现有单卡片 run 的
   `reconcile_orphaned_active_jobs`（`main.py:30`）需扩展到 batch 级。
4. **环检测**：编辑期临时环会让拓扑排序失败，需在子图计算时检测并报错，不静默跑
   出错误顺序。

---

## 6. 验收标准（实现完成后）

- [ ] 重跑卡片 A，A 的所有下游卡片自动变 `stale`（`propagate=all`），前端即时显示
- [ ] `rerun(A, propagate=none)` 只重跑 A，下游不变（单卡片调试语义保留）
- [ ] 选区运行 `from_card=A` 跑通 A→B→C（拓扑序），中间失败可续跑
- [ ] RUN ALL 复用选区运行的调度，行为与现有 autopilot 一致
- [ ] 失效传播遇环不死循环，报 issue
- [ ] `DependencyAttentionService` 仍可查依赖问题（诊断功能不丢）
- [ ] 进程重启后 batch run 状态可恢复

---

> **第一增量实施记录（2026-06-22）**
>
> 本文档原状态为“设计 + 根因分析（未写码）”。第一增量已按 docs/69 的边界完成：
> - §3 事件驱动失效传播：新增 `DownstreamInvalidationService`（`backend/app/services/downstream_invalidation_service.py`），
>   复用 `DependencyAttentionService.build_consumer_edges` 做 BFS 下游遍历；在 `AssetMaterializationService.set_current`
>   调用方 `_finalize_run_review` accept 后主动把下游卡片+资产标 `stale`。
> - `worker_service.rerun_card` 增加 `propagate` 参数（默认 `all`），支持 `all|none|depth:N`；
>   API `POST /cards/{card_id}/rerun` 与 manager-agent `rerun_card` tool 均已透传该参数。
> - §4 形态一（从某卡片跑到末尾）：新增 `SubgraphRunService`（`backend/app/services/subgraph_run_service.py`）
>   与 `POST /projects/{project_id}/runs/subgraph` + `GET .../runs/subgraph/{batch_run_id}`。
>   - 仅支持 `mode=from_card`；计算起点 + 下游闭包，Kahn 拓扑排序，检测到环返回 409 `subgraph_cycle`。
>   - batch 状态持久化在 `graph.metadata["batch_runs"]`：含 `planned_cards/completed_cards/failed_cards/skipped_cards/card_run_ids/status`。
>   - 调度器为后台线程：按就绪集合并发启动卡片（同层可并行），遇 `executor_capacity_full` 重试，
>     `stop_on_fail=True` 时失败即停止并跳过剩余卡片。
>   - 起点卡片用 `rerun_card(..., propagate="none")` 启动，避免与调度器重复失效；后续卡片用
>     `start_run(..., propagate_invalidation=True, batch_run_id=...)`，accept 后自动把下游标 stale。
> - 前端：卡片 Specialist 页面为 stale/accepted/failed/needs_review 等状态新增“重跑本卡（级联下游）”
>   和“从这里跑到末尾”按钮；`CardStatusBadge` 已支持 `stale` 显示。
>
> **探查前提变化（docs/69 已拍板，未照搬本文原字面）**：
> - 无确定性多卡片调度器 / 无 RUN ALL，单卡片运行保持现状，第一增量新建 subgraph 调度，不迁移。
> - 无 batch run 重启自动续跑：活跃 batch 与单 run 一致，重启后由 `_reconcile_active_runs` 标 failed；
>   状态已持久化供前端展示进度，但重启后不自动续跑。
>
> **测试**：`backend/tests/test_downstream_invalidation.py`（11 例）+
> `backend/tests/test_subgraph_run.py`（7 例），全量 `PYTHONPATH=backend python -m unittest discover -s backend/tests`
> 587 例全绿。前端 `npm run build` 通过。
>
> **留给后续增量**：形态二（多选）/ 形态三（起终路径）；batch run 完整重启恢复；RUN ALL 构建 +
> 单卡片调度迁移。

## 7. 与 docs/64 / docs/65 的关系

- 本文的执行模型设计不受打包形态影响（Linux/Docker/macOS 三种部署共用同一套
  `WorkerService` + 失效传播逻辑）。
- 沙箱抽象（docs/64）和依赖链重设计（本文）正交：沙箱管“执行器怎么隔离运行”，
  本文管“跑哪些卡片、跑完状态怎么传播”，互不依赖，可并行实现。
- 定位（docs/65）是本文的前提：因为是工作流 IDE，才需要选区运行和可靠的失效传播；
  若是 autopilot 平台，全图重跑即可，不需要选区。
