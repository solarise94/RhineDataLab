# 后续工作 bundle:§4.1b 收尾 + docs/66 第一增量

> 用法:把本文件整段作为新 agent 会话的第一轮输入。本 bundle 含两个独立任务,
> 按 §顺序执行。每个任务有自己的完成判据。尽量自主推进,减少中途确认;
> 只有遇到本 prompt 明确标注的「产品决策点」时才停下来问。

---

## 你是谁、在哪

你是 Blueprint RE 的实现 agent。仓库根有 `AGENTS.md`(项目规范,必读)、
`docs/67`(silent fallback 审计,#1-#10 已完成并提交于 `0269491`)、
`docs/66`(执行模型重设计,尚未实现)。

关键纪律(摘自 docs/68,继续适用):
- 代码是 source of truth。docs 描述与代码实况不符时,以代码为准,并指出来。
- 不为"干净"引入新抽象。优先用现有 pattern(`atomic_write_json`、`read_json`、
  项目已有的 Result/错误返回风格)。看周围代码怎么写就怎么写。
- 遇到产品决策歧义,先停下来问,列 2-3 个选项让用户选,不要自己拍板。
- 后端测试:`PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests`
- 改了 Pydantic 模型/patch schema 后,跑 `.venv/backend/bin/python scripts/generate_backend_schemas.py`
- manager-agent 改了就 `cd manager-agent && node --check src/server.js`
- **提交纪律:不要自动 commit。完成全部工作后告诉用户,由用户决定提交。**

---

## 任务 B(先做):§4.1b — `_ensure_registry` 损坏输入触发全盘覆盖写

**性质**:外科手术式 bug 修复,和 #1-#10 同节奏。这一轮收掉,让 docs/67 审计
remediation 100% 关闭。

### 背景(docs/67:491-523 已详述,这里是要点)

`backend/app/services/library_registry_service.py`:
- `_ensure_registry`(224-240):文件损坏/schema 不符 → `model_validate` 异常被吞成空
  registry → `not registry.items` 为真 → `refresh_entries` 全盘重扫 →
  `_write_registry` 覆盖磁盘。若某些条目源此刻不可达(源根离线、手注册条目源已移走),
  重扫结果里没它们 → **既有条目被静默抹除(数据丢失)**。
- `_load_registry_items`(589-595)+ `_add_or_replace_entry`(被 install/register 三处调用,
  :123/167/350 一带):损坏 → `[]` → 追加新条目写回 → **用"仅新条目"覆盖整张表**。

**与 §4.1(#4 已修)的区别**:§4.1 是展示层误报(只读不写);§4.1b 是存储层覆盖写
(损坏读触发破坏性写,违反 "reads must not write" + "区分正常空/出错空")。

### 修复方向(docs/67:519-523 给定)

1. `_ensure_registry` 对"文件存在但损坏/schema 不符"应 **raise 或走显式 repair 分支**
   (备份损坏文件 + 重建并告警),而非静默变空触发覆盖。
2. `_load_registry_items` 应区分两种空:
   - **文件缺失**(合法 bootstrap,返回 `[]`)
   - **存在但不可解析**(raise,绝不静默返回 `[]`)
3. 核查 `refresh_entries` / `_build_*_entries` 在损坏输入下的行为。
4. 补 install/register 流程的测试。

### 自主决策边界(这些你可以自己定,不需问)

- **repair 分支的具体形态**:备份损坏文件到 `*.corrupt-<timestamp>.bak` + 写空
  registry + 结构化告警(log + 可选的 issue/notify)。复用项目已有的告警 pattern,
  看周围代码怎么做的。备份命名要避免覆盖既有备份。
- **告警通道**:看 `library_registry_service` 现在用什么日志/通知机制,沿用。
- **测试范围**:至少覆盖 (a) 文件缺失→合法空;(b) 文件损坏→raise/repair,不覆盖写;
  (c) install/register 流程在损坏输入下不抹既有条目;(d) 正常 bootstrap 路径回归保护。

### 产品决策点(只有这些需要停下来问)

**无预期决策点。** docs/67:519-523 的修复方向已足够明确(raise 或 repair 分支)。
如果你在实现中发现"raise 会击穿某个调用方且无法局部修复"——这种情况才停下来问,
附上调用方分析和 2-3 个选项。否则按修复方向直接做。

### 完成判据

- [ ] `_ensure_registry` / `_load_registry_items` 区分正常空 vs 出错空,出错不静默
- [ ] 损坏输入不再触发覆盖写(走 raise 或 repair 备份分支)
- [ ] install/register 三处调用点在损坏输入下不抹既有条目
- [ ] 新增测试覆盖上述场景 + 正常路径回归
- [ ] `PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests` 全绿
- [ ] docs/67 §4.1b 追加实施记录 + 优先级列表标 ✅ + 从 trailing HIGH 列表移除

---

## 任务 A(后做):docs/66 第一增量 — 失效传播 + 选区运行形态一

**性质**:新功能开发。docs/66 有详尽设计(§3 失效传播、§4 选区运行、§5 实现影响、
§6 验收标准)。本任务**只做第一增量**,不要试图一次吞下整个 docs/66。

### 第一增量的边界(严格遵守)

只做 docs/66 的 **§3(事件驱动失效传播)+ §4 形态一(从某卡片跑到末尾)**。
**不做** 形态二(多选)/形态三(起终路径)——那是后续增量。
**不做** 前端选区 UI 的复杂交互——前端只做"stale 态展示"+"从这里跑到末尾"按钮。

分两步,每步独立可测:

**步骤 A1:失效传播(§3)** — 基础,选区运行依赖它
- 新增 `DownstreamInvalidationService`:封装"从某卡片起,把下游卡片+资产标 stale",
  复用 `DependencyAttentionService.affected_downstream` 的图遍历(docs/66:273)。
- `AssetMaterializationService.set_current` 的调用方:supersede 旧资产后,调
  `DownstreamInvalidationService.invalidate_from(producer_card)`(docs/66:274)。
- stale 语义精确化(docs/66:148-161):stale 卡片保留上次产出资产(标 stale,不删),
  用户可对比新旧。
- `worker_service.rerun_card` 增加 `propagate` 参数,默认 `all`(docs/66:275)。

**步骤 A2:选区运行形态一(§4)** — 依赖 A1
- 新增 `POST /projects/{project_id}/runs/subgraph`(docs/66:203),形态一:
  指定起点卡片,跑它 + 所有下游(拓扑序)。
- `WorkerService` 支持 batch run 调度:拓扑序 + 并发 + stop/continue-on-fail
  (docs/66:276)。**单卡片运行和 RUN ALL 复用这套调度**(docs/66:255)。
- 前端:stale 态展示 + "从这里跑到末尾"按钮(最小交互)。

### 自主决策边界(这些你可以自己定)

- **`DownstreamInvalidationService` 的实现细节**:遍历算法、并发控制、stale 标记的
  数据结构 —— 看 `DependencyAttentionService.affected_downstream` 现在怎么做,复用。
- **batch run 的持久化**:docs/66:301-302 提到"持久化 batch_run_id 及 planned_cards"。
  具体存哪个文件、schema 怎么设计,你看现有的 run 持久化(`graph/runs.json`)怎么做,
  沿用同一套 pattern。
- **环检测**(docs/66:303):编辑期临时环让拓扑排序失败时,报错不静默。具体报错形式
  (issue/raise/error response)看周围代码 pattern。
- **stale 资产清理时机**:docs/66:297-300 建议"保留到下次成功 run 后 supersede,不主动删"。
  按这个做。
- **并发度**:batch run 内的卡片并发,看现有 RUN ALL 怎么做的,沿用。

### 产品决策点 — 已由用户拍板(2026-06-22),按此执行,不再问

探查发现(docs/66 前提已变,实施时以此为准,不照搬 docs/66 字面):
- **没有确定性多卡片调度器**。每个 run 单独 `start_run`,并发满了直接 HTTP 409 拒绝(无队列)。
  AUTO 模式是 agent 驱动(evaluate→wake),不是拓扑调度器。
- **没有 RUN ALL**(前后端都没有)。docs/66:255"单卡片运行和 RUN ALL 复用这套调度"描述的是
  一个还不存在的目标 —— **没有东西可迁移**。
- **系统对单 run 也没有任何重启恢复**:`_reconcile_active_runs` 把任何活跃 run(queued/launching/
  running/reviewing)直接标 failed。批量运行是一串这样的 run。
- **"stale" 状态已端到端支持**(backend CardStatus/AssetStatus + 前端 Card 类型 + badge),
  前端 stale 展示基本免费。

**决策 1 — 中断恢复:持久化状态,不自动续跑**
持久化 batch_run 状态(batch_run_id + 每卡进度),前端能显示"哪些跑了/哪些失败/哪些待跑"。
但**重启后不自动续跑**:沿用现有单 run 行为(活跃 run 标 failed),用户手动从断点重新"跑"。
理由:与"单 run 不自动恢复"现状语义一致。完整重启恢复(启动时检测被中断的 batch run 并
重派发)留增量二 —— 那时应该单 run 和 batch run 一起设计恢复,而不是只给 batch 做。

**决策 2 — 调度范围:新建 subgraph 调度,不迁移**
第一增量就是**新建** subgraph 调度(拓扑序 + 并发 + stop/continue-on-fail),服务
`POST /runs/subgraph`。**单卡片运行保持现状**(单独 start_run),不动。不建 RUN ALL,
不迁移单卡片到新调度 —— 那是后续增量。
docs/66:255"复用 + 迁移"在第一增量不适用(RUN ALL 不存在),在实施记录里标注此前提变化。

**全级联 stale**:按 docs/66 §3.3 设计(默认全级联 + 可配上限),自主做,不问。

### 范围边界 — 不做的事

- **不做 macOS 兼容**(用户无开发者账号,暂不做)。不碰任何 macOS/seatbelt/Electron 相关代码。
- 不做形态二(多选)/形态三(起终路径)—— 后续增量。
- 不做完整重启恢复(见决策 1)。
- 不建 RUN ALL、不迁移单卡片调度(见决策 2)。
- 前端只做最小交互:stale 态展示 + "从这里跑到末尾"按钮 + batch 进度显示。

**除以上明确范围外,不要为设计细节问。** docs/66 的设计文档 + 上面探查发现足够指导实现;
没覆盖的实现细节,按"看周围代码 pattern"自主决定。

### 完成判据(docs/66 §6 验收标准的子集)

第一增量必须满足:
- [ ] 重跑卡片 A,A 的所有下游卡片自动变 stale(`propagate=all`),前端即时显示
- [ ] `rerun(A, propagate=none)` 只重跑 A,下游不变(单卡片调试语义保留)
- [ ] 选区运行形态一 `from_card=A` 跑通 A→B→C(拓扑序),中间失败可续跑
- [ ] 失效传播遇环不死循环,报错(issue/raise)
- [ ] `DependencyAttentionService` 仍可查依赖问题(诊断功能不丢)
- [ ] 全量测试绿;改了模型就 regen schema

留给后续增量(不在本任务):
- 形态二(多选)/形态三(起终路径)
- batch run 完整重启恢复(见决策 1)
- RUN ALL 构建 + 单卡片调度迁移(见决策 2)

---

## 执行顺序与汇报

1. **先做 B(§4.1b)**,做完跑测试,写 docs/67 记录。B 完成后告诉我一声(简短),
   然后继续 A —— 不要等确认,B 是自主范围。
2. **再做 A(docs/66 第一增量)**。开始 A1 前先读 docs/66 全文 + 核查
   `AssetMaterializationService.set_current`、`DependencyAttentionService.affected_downstream`、
   `worker_service.rerun_card` 的现状。A1 做完跑测试。
3. **A 的产品决策点已由用户拍板(见上文"产品决策点"节),按决策执行,不再问。**
   实现中遇到 docs/66 和探查发现都没覆盖的**真·产品歧义**才停下来问(列 2-3 选项);
   纯实现细节自主决定。
4. 全部完成后,给我一份完成报告(改了什么、测试结果、docs 记录、与拍板决策的偏差),
   **不要自动 commit**。

现在开始:读 AGENTS.md、docs/67 §4.1b、docs/66,然后开始 B。开始前用一句话告诉我
你对 §4.1b 的实现计划。
