# 67. 静默 Fallback 与硬编码审计：被掩盖的 Bug

Status: audit report (read-only findings, no code changed). 供后续修复排期。

Date: 2026-06-21

Related:

- `docs/65_product_positioning_workflow_ide.md`（定位锚）
- `docs/66_execution_model_and_dependency_chain_redesign.md`（依赖链重设计，
  本文若干发现是其根因的具体表现）

## 背景

系统早期为"能跑起来"引入了大量 fallback 和硬编码。这些代码的共同危害是：
**失败被静默降级，降级后的结果与"正常结果"在调用方看来无法区分**，于是真实的
功能性 bug 被掩盖，用户看到的是错误现象（"找不到技能""运行失败""结果不对"）而非
根因（"配置坏了""数据损坏""状态机违例"）。

本文档把审计发现按**掩盖的 bug 类型**归类（而非按代码模式堆叠），便于修复时按危害
排序。每条给出 `file:line`、模式、掩盖的 bug、严重度。

严重度定义：

- **HIGH**：掩盖功能性 bug，用户会直接踩到且无法定位根因
- **MED**：掩盖数据/schema 漂移或边缘情况，偶发且难复现
- **LOW**：防御性合理，或仅影响可观测性，保留可接受

---

## 一、数据损坏被静默修复（最危险：read 改写持久化状态）

这类问题的共同形态：**读取操作触发对持久化状态的"修复"写回**，原始损坏被抹掉，
且修复用的是启发式猜测而非确定性逻辑。

### 1.1 [HIGH] 资产绑定丢失后，读取时用"最佳候选"启发式重建

`backend/app/services/asset_materialization_service.py:78-149` `bootstrap_from_aliases` +
调用点 `project_service.py:952-957`（快照读取时）+ `worker_service.py:809-810`（run accept 时）。

**模式**：当 `graph.metadata["asset_materializations"]` 缺失时，从
`Asset.metadata["planned_asset_id"]` 反推绑定，用 `_pick_best_candidate`（status_rank +
run order）选具体资产，然后**写回 graph 并持久化**。

**掩盖的 bug**：绑定表丢失本应是个硬错误（写失败/损坏），但被静默重建为"最佳猜测"。
两个 run 产出同 alias 的资产时，可能绑定到错误的具体资产 → 下游读到旧数据，无任何报错。
而且**读操作改写了持久化状态**，破坏了读写的幂等性，使问题更难复现。

**与 docs/66 的关联**：这正是依赖链问题的温床——绑定关系不可信，失效传播就无从谈起。

### 1.2 [HIGH] 项目注册表损坏，静默降级为文件系统扫描

`backend/app/services/project_service.py:75-87`

```python
except RuntimeError:
    # Registry corrupted — fall through to legacy
    pass
return project_root(self.settings.data_root, project_id)
```

**掩盖的 bug**：注册表损坏（`RuntimeError` 被吞）后静默走 legacy 文件扫描路径，项目可能
解析到错误/不一致的 root。注册表损坏本身只 `logger.error`，不阻断、不修复、不告警。

### 1.3 [HIGH] graph.json 快照恢复失败被吞，可能留下半恢复状态

`backend/app/services/patch_apply.py:319-322`

```python
path.write_bytes(payload)   # ← 非原子写入
...
except Exception:
    return False
```

`_restore_snapshot` 在 patch 失败后回写 graph/runs/report/cleanup。两个独立问题叠加：

1. **异常被吞**：若回写本身失败（磁盘满、权限、部分写入），异常被吞、返回 `False`。
   调用方只把 project 标 `error`，不知道恢复失败的原因，也不知道 graph.json 是否处于
   半写入的损坏状态。
2. **非原子写入**（核查补充）：第 319 行用 `path.write_bytes(payload)`，**而非项目自有的
   `atomic_write_json`**（`utils.py:15`，`tempfile` + `os.replace`）。同文件第 14 行已
   `import atomic_write_json`，第 183/218 行别处都在用——唯独恢复路径用裸 `write_bytes`。
   这意味着**即使不抛异常，中途 crash 也会留下截断文件**。

**掩盖的 bug**：下一次 load 可能读到损坏的 graph（截断的 JSON 或半写入的字节）。这是本次
审计**最危险的单点**——异常吞咽 × 非原子写入双重风险。

**修复要求**（两条一起改）：
- 加 `logger.exception` 记录恢复失败的具体异常，调用方能区分"恢复失败"与"无需恢复"。
- `write_bytes` 换成 `atomic_write_json`（或等价原子写入），消除截断风险。
- **测试覆盖**（核查补充）：`backend/tests/` 搜索 `_restore_snapshot` / `restore_snapshot`
  均无结果，零覆盖。修复时必须同步补测试（含"恢复中途 crash 不留截断文件"的用例）。

### 1.4 [HIGH] 读快照时，runtime_preferences 损坏静默回退默认值

`backend/app/services/project_service.py:1061-1068`

```python
try:
    project = project.model_copy(update={... model_validate(runtime_preferences)})
except Exception:
    pass
```

**掩盖的 bug**：`runtime_preferences` 元数据损坏 → 项目静默加载默认/空偏好 → 卡片在错误的
Python/R runtime 下启动，无日志。用户困惑"为什么用了 base 而不是 omicverse"。

---

## 二、状态机违例被静默 coerce（状态污染传播）

这类问题：**不该出现的状态被"修正"成某个合法状态**，而非报错。状态机的约束被绕过，
非法状态被洗白。

### 2.1 [HIGH] plan revision 把任意状态 coerce 成 planned

`backend/app/services/manager_blueprint_tools.py:3111-3121` `_apply_plan_revision_status`

```python
if updated.status in {"running","reviewing"}:
    return updated
updated.status = "planned"   # ← 其余任何状态静默变 planned
```

**掩盖的 bug**：对一张 `failed` / `needs_review` / `accepted` 的卡片做 plan revision，
状态被静默重置为 `planned`。`needs_review` 卡片未解决 review 就被重置，review 阻塞态丢失。
调用方完全不知道状态被改。

**实现记录（#6，已完成）**

修复原则：保留「revise → 回 planned」这一**正确**的重置语义（旧计划下的 run 结果确实
已失效），但把静默 coerce 变为**显式上报**，不改变重置行为本身。

- `_apply_plan_revision_status` 改签名为 `(previous, updated) -> tuple[Card, list[dict]]`：
  仅当发生真实 coerce（previous 既非 `planned` 也非 `running`/`reviewing`）时返回非空
  `plan_revision_warnings`，结构 `[{previous_status, coerced_to:"planned", reason}]`。
- 审计 note **append** 进 `manager_review`（复刻 `annotate` 的 `manager_review_append`
  幂等 idiom），不再无条件覆盖——既有人工/AI review 文本不被污染，同一 note 不重复追加。
- `update_card` 把该 warnings 接进返回值 `result["plan_revision_warnings"]`（仅 coerce 时出现），
  Manager-AI 由此得知「pending review/acceptance 已作废」。

**刻意未碰（边界，留给 §2.x 状态机条目）**：keep 分支只认 `{running, reviewing}`，
而 `graph.py:22-24` 的 `ACTIVE_RUN_STATUSES` 是 5 元（另含 `queued`/`launching`/`needs_approval`）。
这个不对称的根因是 **card.status 与 run.status 两套权威源不一致**——属于完整状态转移表
（§2.x 状态机）的范畴，不是 §2.1 的「静默」缺陷，#6 不在此扩面。`running`/`reviewing`
走 `return updated` 是 keep 而非 coerce，本就不在 §2.1 射程内，不产生 warning。

测试：`backend/tests/test_plan_revision_status.py`（此前零覆盖），10 例——`_apply_plan_revision_status`
单元（planned 保持 / running·reviewing keep / needs_review·accepted coerce / append 保留既有
review / note 去重幂等）+ `update_card` 端到端验证 `plan_revision_warnings` 接线。全量 551 测试
全绿（本机环境）；用户 dev shell 若导出 provider key，仍有 1 个 pre-existing 失败
（`test_clearing_default_provider_key_clears_legacy_secret`，env fallback 复活已清 secret），
与 #6 无关，属 §3.5/#8 待办。

### 2.2 [HIGH] 模块组状态：未知子状态组合 → planned/mixed

`backend/app/services/module_group_state_service.py:59-72` `_derive_group_status`

```python
return "planned", "mixed"   # ← 兜底分支
```

**掩盖的 bug**：子卡片处于非预期状态组合时，组状态报 `planned`，下游看到"可运行"的组
实际并不可运行。

### 2.3 [HIGH] 重启时在途 run 被强标 failed（含正常推进中的）

`backend/app/services/worker_service.py:3124-3167` `_reconcile_active_runs`

```python
if run.status not in {"queued","running","reviewing"}:
    continue
thread = self._threads.get(run.run_id)
if thread and thread.is_alive():
    continue
run.status = "failed"
run.summary = "Backend restarted before executor completed; run marked failed during reconcile."
```

**掩盖的 bug**：重启时，任何非终态且无活跃线程的 run 被标 `failed`。但"无活跃线程"不一定
意味着"已死亡"——多进程/分布式场景下线程句柄未重新注册，正常推进中的 run 被误杀并标失败，
卡片也被 coerce 成 `failed`。合成 summary 掩盖了真实原因。

**与 §5.1 的关联（审计纠错，二次修正）**：此处的活跃集 `{"queued","running","reviewing"}`
缺口分析，经过代码核查（run 生命周期：`start_run` 抢占式获 slot → 创建 run → 同调用内
`thread.start()`；线程首动作置 `launching` → `Popen`；全程无延迟队列、无 pump、无重启
重调度）确认如下。修复时必须按 **run 状态的"磁盘可恢复性"** 判断，不能按"有无线程"
一刀切，也不能机械照搬 `_active_run_statuses()` 5 元全集：

| 状态 | 重启后语义 | reconcile 应否处理 | 理由 |
|------|-----------|-------------------|------|
| `queued` | **孤儿**：线程在原进程内，重启后无 pump/重调度重新拉起 | **应处理**（标 failed） | `start_run` 创建 queued run 后同调用内 `thread.start()`（line 462-479），queued 只在"落盘→线程跑到第一行"的转瞬窗口存在。重启后无线程的 queued run 永远卡占，是和 launching 同类的幽灵活跃 run |
| `launching` | **孤儿**：已 `Popen`（line 1106），进程孤儿、线程句柄丢 | **应处理**（标 failed） | 有进程有线程，重启后进程孤儿，必须被 reconcile 清理 |
| `running` | **孤儿**：执行中，进程/线程状态同上 | **应处理**（标 failed，现状已处理） | 同 launching |
| `reviewing` | **孤儿**：执行器已退，但 review 未完成 | **应处理**（标 failed，现状已处理） | 现状正确 |
| `needs_approval` | **可恢复**：纯等待态，未起进程无线程 | **绝不能处理** | run 创建时（line 377）等用户批准，未 Popen、无线程。重启后用户继续审批即可从磁盘恢复。若加入 reconcile 会误杀所有"正等用户批准"的暂停 run |

**结论**：reconcile 应处理集 = `_active_run_statuses()` 减去 `needs_approval` =
`{queued, launching, running, reviewing}`。`needs_approval` 是**唯一**该排除的非终态
（因为只有它具备"重启后可从磁盘恢复"的语义）。

**判断框架（给实施）**：不是"有无线程"——`queued` 无线程但该处理（孤儿），`needs_approval`
也无线程但不该处理（可恢复）。正确判据是**"重启后这个状态能否自己往前推进"**：
- 不能推进、且没有外部恢复机制（pump/重调度/用户审批流）→ 孤儿，reconcile 标 failed
- 能自己推进（用户审批 / 磁盘状态机）→ reconcile 不碰

**实现记录（#7，2026-06-22）**：

§2.3 的**核心状态集缺口**（`{queued,running,reviewing}` 漏掉 `launching`，且需排除
`needs_approval`）已在 §5.1（#3）通过 `RESTART_ORPHANED_RUN_STATUSES =
{queued, launching, running, reviewing}` 常量交付（`worker_service.py:3214` 已用此常量过滤）。
#7 处理 §2.3 剩余的**真实 bug**：reconcile 把孤儿 run 标 failed 时**盲写**合成 summary，
从不读取执行器已落盘的 terminal report，于是：
- 执行器重启前已写真实失败报告（`report_fail`/`synthetic_failure` + `executor_failure.json`）
  的 run，被通用 "Backend restarted…" 文案掩盖真实原因；
- 更糟：执行器**已完成**（`report_complete`，磁盘 `status=pending_review`，
  见 `command_worker.py` complete 子命令）但后端在 finalize 前重启的 run，被盲标 failed +
  通用文案，且后续误发失败通知——用户无从得知"其实跑成功了，只是没 finalize"。

**修复（外科式保真，worker_service.py）**：
1. 新增 `_reconcile_run_summary(project_id, run_id)`（`worker_service.py:2240`）：读取磁盘
   terminal report 分类生成 summary——`report_fail`/`synthetic_failure` → 保留执行器真实
   summary（优先 `executor_failure.json`，回退 terminal_report.summary）；`report_complete`
   → 诚实文案 "Executor reported completion before the backend restarted; the run could not
   be finalized during reconcile and was marked failed. Re-run the card to recover its
   result."（仍标 failed，因为启动时**不**重放 finalization，见 §2.3b）；无报告 → 保留原
   通用文案（真正在途孤儿的真实原因）。
2. 删除 `_reconcile_active_runs` 循环里的 `thread.is_alive()` 残留判断（reconcile 只在
   `__init__` 早期跑，`self._threads` 恒空，该判断是死代码），改为注释说明。
3. card 侧 `manager_review` 由**覆盖**改为**追加**（annotate 幂等惯用法，与 #6 一致）：
   保留既有人工/AI review，去重追加 summary，使 `card.status=failed` 与 review 文本语义一致
   （failed = "此 run 结果不可用"，非"执行器没跑成功"）。
4. 测试 `backend/tests/test_reconcile_active_runs.py` +6：保真失败报告、synthetic 回退、
   report_complete 诚实重跑文案（非通用、非 success）、真正孤儿通用文案、review 追加不覆盖、
   reviewing 卡 coerce 到 failed（探针发现的覆盖缺口）。全量 557 passed / OK。

### 2.3b [HIGH] 重启后不重放 finalization：已完成 run 的成功恢复（#7 拆分）

`backend/app/services/worker_service.py` `_reconcile_active_runs` /
`_reconcile_run_summary`（report_complete 分支）

**现状（#7 后）**：执行器写完 `report_complete`（manifest 待 review）但后端 finalize 前
重启的 run，reconcile 诚实标 failed 并提示用户重跑。这是**保真**的——不再谎称成功，也不再用
通用文案掩盖——但**不是恢复**：用户必须手动重跑一次本已算完的卡片。

**待办**：启动时为 `report_complete` 孤儿**重放 finalization**（manifest 审计 / 校验 / review
/ asset 物化 → 正常进入 `reviewing`/`needs_review`），而非标 failed。属高风险（finalize 链
含多步副作用与通知路径，且 reconcile 在 `inject_wake_dispatch()` 之前跑，通知须走
`_reconciled_run_ids` 批延迟），按外科手术原则单独立项，不在 #7 范围内。


### 2.4 [HIGH] ManagerAuto 状态在每次 load 时静默迁移

`backend/app/models/manager_auto.py:51-98` `migrate_legacy_fields`

```python
state_map = {"active":"pending_wake","thinking":"running","blocked":"idle",...}
if old_state in state_map:
    data["state"] = state_map[old_state]
```

**掩盖的 bug**：每次加载都把旧状态值重写到新状态机，无审计痕迹。被旧版本错误持久化的
状态会被"洗白"成新状态机的合法值，卡死/中止的 session 可能复活为 `idle` 重新派发。
还无条件把 `max_chain_count` 提到 50、丢弃 legacy 字段。

### 2.5 [MED] 卡片 status 缺失/空 → planned

`backend/app/services/manager_blueprint_tools.py:3027`

```python
status: payload.get("status") or "planned"
```

**掩盖的 bug**：Manager-AI 产出 `status: null` / `status: ""`（契约违例）被静默当作新
planned 卡片，而非报"payload 非法"。

### 2.6 [MED] stop reason 静默重映射

`backend/app/services/manager_auto_service.py:241-245`

```python
if reason == "auto_once_complete":
    next_state.stop_reason = "user_stop"
```

**掩盖的 bug**：一次性完成（auto）被记成用户停止（user），审计历史里两者混淆。

---

## 三、配置错误被静默兜底（误配置表现为功能 bug）

这类问题：**配置缺失/错误时静默用默认值**，用户看到的是功能异常而非"配置错了"。

### 3.1 [HIGH] 所有角色默认 provider 都是 deepseek

`backend/app/services/app_config_service.py:19-24`

```python
DEFAULT_PROVIDER_BINDINGS = {
    "manager": {"provider_id": "deepseek"},
    "reviewer": {"provider_id": "deepseek"},
    "pi_executor": {"provider_id": "deepseek"},
    ...
}
```

**掩盖的 bug**：用户只配了 Anthropic key，但 binding 仍说 deepseek → 静默 401，用户以为是
"功能坏了"而非"binding 没改"。且 `app_config_service.py:230-231` 的 `or "deepseek"` 在
resolved provider 已有不同 id 时仍回退 deepseek，掩盖 binding 解析 bug。

**跨语言双重硬编码**（核查补充）：Node sidecar `manager-agent/src/server.js:15` 也独立写
`const PROVIDER = process.env.MANAGER_AGENT_PROVIDER || "deepseek"`。Python 侧集中常量时
**必须 Python + JS 两侧一起改**，否则只改一侧仍不一致。同理 `server.js:16` 的
`MODEL ... || "deepseek-v4-pro"` 与 Python 侧模型默认值也是跨语言双份。

**实现记录（#8，2026-06-22）**：

核查澄清——本项**不**改变产品默认 provider（deepseek 仍是产品的预期默认值，config.py /
server.js / 默认 profile 全部一致），也**不**碰 key 缺失→空串那层（属 §3.5/#10）。#8 做三件事：

1. **集中默认 provider 常量**（fix 原则 d）：`app_config_service.py` 新增模块常量
   `DEFAULT_PROVIDER_ID = "deepseek"`，`DEFAULT_PROVIDER_BINDINGS` 五角色与
   `manager_agent_config` 的运行时 provider 兜底（`MANAGER_AGENT_PROVIDER or DEFAULT_PROVIDER_ID`）
   都引用它。Node 侧 `server.js` 同步新增 `const DEFAULT_PROVIDER_ID = "deepseek"` 供
   `PROVIDER` 使用，并加交叉引用注释（两 runtime 各持一份字面量，跨语言一致靠"改一处必改另一处"
   的注释约束，无共享配置源——这是 §3.1 核查已认定的现实约束）。

2. **删除静默兜底（真正的 silent-fallback bug）**：`manager_agent_config:231` 原
   `manager_provider.get("provider_id") or "deepseek"` 改为 `manager_provider["provider_id"]`。
   核查确认 `manager_provider` 必来自 `_require_api_provider`——它只返回 sanitize 过、
   `provider_id` 必非空的 profile，否则直接 raise（`_sanitize_api_provider_profiles` 在
   `provider_id` 为空时丢弃该 profile）。故 `or "deepseek"` 只能在"不可能的"畸形态触发，是
   纯掩盖代码：删除后该不变量若被破坏会 KeyError 显式炸出，而非被静默改写成 deepseek。
   （注：`:230` 的 `provider` 是给 Node sidecar 的运行时 provider 名，是合法默认而非掩盖，
   保留并改用常量；`:226` base_url、`:245` key 的 fallback 链属 §3.5 key 层，不在 #8 范围。）

3. **KI-1（env 耦合，用户裁决＝测试隔离 env）**：`_effective_*_api_key` 的
   `os.environ[...]` 兜底是**合法的配置层**（UI > settings > env），保留不动；问题仅是
   `test_clearing_default_provider_key_clears_legacy_secret` 依赖了环境干净度。改为在该测试内用
   `patch.dict(os.environ, clear=False)` + pop 显式清除 `BLUEPRINT_DEEPSEEK_API_KEY` /
   `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`，使断言只反映配置状态、与开发者 shell 里 export 的值
   解耦（退出时自动恢复）。其余断言显式 key 的测试不受 env 影响，无需隔离。

**测试**：`test_executor_profiles.py` +1（`test_manager_agent_config_selected_provider_id_reflects_binding`，
回归保护 mask 删除：manager 绑到非 deepseek provider 时 `selected_provider_id` 必回显真实绑定）；
`test_clearing_default_provider_key_clears_legacy_secret` 加 env 隔离。在 **provider env 全部
export 的环境下**该文件 46 tests 全 OK（旧代码此环境下会 fail 目标测试），全量套件 OK。Node
`node --check src/server.js` 通过（sidecar 无单测套件）。

> **遗留（不在 #8）**：`server.js:16` 的 `MODEL || "deepseek-v4-pro"` 与 config.py 模型默认值
> 跨语言双份——模型默认值真正落在 config.py 并经 `manager_config` 下发，`server.js:16` 仅是
> 独立运行时的兜底，且涉及 §3.2/§3.6 模型默认值条目，留待那些条目统一处理。

### 3.2 [HIGH] OpenAI profile 硬编码 gpt-4o-mini

`backend/app/services/app_config_service.py:470`

```python
"model": "gpt-4o-mini",
```

**掩盖的 bug**：deepseek/anthropic profile 读 `config.get("manager_model")`，唯独 OpenAI
硬编码。UI 改了 manager_model，OpenAI 调用仍用过时模型，用户无法理解"为什么没生效"。

### 3.3 [HIGH] opencode renderer 硬编码 gpt-4o-mini / anthropic / openai 兜底

`backend/app/workers/provider_renderers/opencode.py:127,140,147,321`

```python
or getattr(settings, "executor_model", "gpt-4o-mini")
provider_id = getattr(profile, "provider_id", None) or "anthropic"
```

**掩盖的 bug**：profile 配错（无 provider_id）时，renderer 静默冒充 anthropic/openai，
run 在 API 层 401，用户看到的是"执行失败"而非"profile 配置不完整"。

### 3.4 [HIGH] CORS 硬编码 13001 端口

`backend/app/main.py:63-64`

```python
"http://127.0.0.1:13001",
"http://localhost:13001",
```

**掩盖的 bug**：`frontend_origin` 是 env 可配的，但这两个字面量不是。前端换端口后 CORS
静默拒绝；或端口没换但 operator 设了不同 `frontend_origin`，请求仍被放行——配置与行为
脱钩。（docs/64 的端口模板化已覆盖此项。）

**处置记录（#10，2026-06-22）——延期，归属 docs/64**：

核查确认 bug 属实。当前 `backend/app/main.py:59-67` 构造
`allowed_frontend_origins = [settings.frontend_origin, "http://127.0.0.1:13001",
"http://localhost:13001"]`——`frontend_origin` 默认 `:3000`，而两个硬编码字面量是 `:13001`
（网关端口），二者恒不一致地被并入白名单。

**但此项已被 docs/64 §1.4 显式认领**（lines 286-288 逐字："同步处理
`backend/app/main.py:59-67` 的 CORS 硬编码：把字面量改成基于 `settings.frontend_origin`"），
并列在 docs/64 "共享重构清单" item 3 与工作量表。该修复**与 nginx 端口占位符化 +
`deploy_user_systemd.sh`/`deploy_release.sh` 的端口渲染强耦合**：单独在此摘掉字面量会
（a）与 docs/64 计划冲突，（b）若部署脚本未同步设 `BLUEPRINT_FRONTEND_ORIGIN=<网关端口>`，
生产 CORS 会立即失效。**用户裁决：延期，随 docs/64 §1.4 一并修，不在本审计 #10 内单独改。**

**处置更新（2026-06-22，docs/70 轮次）——已关闭（CLOSED），由 docs/64 §1.4 落地**：

CORS 硬编码已随 docs/64 §1.4 共享重构一并修复：
- `backend/app/main.py` 移除 `http://127.0.0.1:13001` / `http://localhost:13001` 字面量，
  改为基于 `settings.frontend_origin` 派生（保留 127.0.0.1 与 localhost 互替形式）。
- `deploy/nginx/blueprint-re.conf.template` 端口占位符化
  （`__NGINX_LISTEN__` / `__FRONTEND_PORT__` / `__BACKEND_PORT__`）。
- `deploy_user_systemd.sh` / `deploy_release.sh` 渲染时填默认端口，且二者已写
  `BLUEPRINT_FRONTEND_ORIGIN=http://127.0.0.1:13001`，与 CORS 派生值一致——Linux 现状
  行为不变，改端口后 CORS 自动跟随，配置与行为重新对齐。

§3.4 自此从 deferred 转为 **closed**。详见 docs/64 §1.9 实施记录。

### 3.5 [HIGH] Tavily / Anthropic / OpenAI key 缺失静默成空串

`backend/app/services/app_config_service.py:74,242,268,446,454`

```python
return os.environ.get("ANTHROPIC_API_KEY", "").strip()  # → ""
```

**掩盖的 bug**：key 缺失变空串，下游 `bool(key)` 判"未配置"，但根因（env 没设）不可见。
DeepSeek 路径至少查 `settings.deepseek_api_key`，这两个跳过 settings 层直接读 env——
不一致，且 settings 与 env 不一致时静默用 env。（注：`BLUEPRINT_INTERNAL_TOOL_TOKEN`
是唯一正确处理的配置项——缺失即报错，是正面范例。）

**处置记录（#10，2026-06-22）——核查发现 finding 已过期，关闭无需改码**：

审计原文的核心技术指控「ANTHROPIC/OPENAI 跳过 settings 层直接读 env」**当前代码已不成立**。
四个 `_effective_*_api_key` 方法统一收口所有 key 的 env 读取（无消费方独立读 `*_API_KEY`）：

| key | 方法 | 层次 | 现状 |
|---|---|---|---|
| DeepSeek | `_effective_deepseek_api_key:433` | config → **settings** → env | 三层 |
| Anthropic | `_effective_anthropic_api_key:448` | config → **settings** → env | 三层 ✅（审计称跳过 settings——已过期） |
| OpenAI | `_effective_openai_api_key:456` | config → **settings** → env | 三层 ✅（同上） |
| Tavily | `_effective_tavily_api_key:442` | config → env | 两层（config.py 无 `tavily_api_key` 字段；`@staticmethod`） |

Anthropic/OpenAI 已在 `:452`/`:460` 查 `self.settings.*_api_key`，与 DeepSeek `:437` 完全同构——
审计写作时尚未如此，代码已演进修复。唯一残留的两层是 Tavily，而 Tavily：
（1）**有意 env-only**（`config.py` 无 settings 字段，是 sidecar 能力而非 provider profile）；
（2）**可选**（`docs/for_agent_install.md` + `install_blueprint_re.sh` 均注明"可后配"，sidecar 显式回显
"Web search is disabled"）。`缺失 → ""` 对可选 key 是**合法的"未配置/禁用"信号**；对必需的 DeepSeek，
缺失已在用例点强制（installer 失败、`pi_worker`/`pi_agent_executor` raise）。

**关键反证**：若在 `_effective_deepseek_api_key` 内改成缺失即 raise，会**击穿
`get_public_settings`**——它正是调用该方法算 `"api_key_configured": bool(key)` 来给前端显示
配置状态；raise 会让设置页 500 而非显示"未配置"。故当前"返回空串、由 `bool()` 报未配置、在
用例点强制"的设计对状态展示路径是正确的。**用户裁决：核查认定 finding 已过期，关闭，不改码；
仅留此记录。**（可选的 Tavily settings 字段对齐为独立增强，本次不做。）

### 3.6 [HIGH] pi_agent_executor 五个内联默认值与 config.py 重复

`backend/app/workers/pi_agent_executor.py:280-284`

```python
base_url = os.environ.get("BLUEPRINT_DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/anthropic")
model = os.environ.get("BLUEPRINT_EXECUTOR_MODEL", os.environ.get("BLUEPRINT_MANAGER_MODEL", "deepseek-v4-flash"))
```

**掩盖的 bug**：五个字符串默认值必须与 `config.py:107-116` 字节级一致，已是漂移风险。
还硬编码了 `/anthropic` 端点路径。config 默认值改了，这里不改 → 执行器用过时值。
`provider_renderers/pi.py:40-51` 有同样的第二份副本。

---

## 四、异常被吞成"空结果"（调用方无法区分"正常空"与"出错空"）

这类问题的核心反模式：`except Exception: return <空>`，其中空值与合法的空结果不可区分。

### 4.1 [HIGH] 注册表加载失败 → 空集 → 全员"技能缺失"

`backend/app/services/package_service.py:542-552` + `library_registry_service.py:593-594`

```python
try:
    skill_registry = self.library_registry_service._ensure_registry("skill")
    skill_ids = {item.id for item in skill_registry.items}
except Exception:
    skill_ids = set()      # ← 注册表出错 = 所有技能"不存在"
```

**掩盖的 bug**：注册表抛异常（锁竞争、JSON 损坏、IO）→ `skill_ids` 变空 → **每个** required
skill 都报"not found" blocker。用户被通知"已安装的技能缺失"，真正原因是注册表加载失败，
但 blocker 文案从不提示注册表错误。

**#4 实施记录（2026-06-22，仅 site 1）**：已修 `package_service._resolve_capabilities`——
两处 `except Exception: skill_ids/mcp_ids = set()` 改为 `logger.exception` + 一条诚实 blocker
`"Registry load failed for {kind}: {exc}; capability check skipped."`，并用 `None` 哨兵区分
"加载失败"（跳过该域检查，不误报）与"加载成功但真为空"（仍正确报 not found）。新增
`backend/tests/test_package_capabilities.py`（6 例）。`logger` 此前在该文件未定义，一并补上。

**审计纠错（核查后）**：原文把 `library_registry_service.py:593`（`_load_registry_items`
损坏 → 返回 `[]`）当作"同一类问题"。核查证明这是**误判**——`:593` 只是内部 helper，真正
危险的不是它返回空，而是 **`_ensure_registry` 在损坏输入下会触发覆盖写**（见新 §4.1b）。
`:593` 的字符层损坏其实在 `read_json` 就已抛出（在 try 之外），只有"合法 JSON 但 schema 不符"
才落入 `except → []`；而那条路径的真正后果是 `_add_or_replace_entry` 静默丢失既有条目。
故 `:593` 不在 #4（展示层）范围，归入 §4.1b（存储层）。

### 4.1b [HIGH] `_ensure_registry` 损坏输入触发全盘覆盖写（数据丢失）✅ 已完成

`backend/app/services/library_registry_service.py:224-240`（`_ensure_registry`）
+ `:589-595`（`_load_registry_items`）+ `:242,123`（`_write_registry` / `refresh_entries`）

```python
def _ensure_registry(self, kind):
    payload = read_json(path, {})            # 缺失→{}；字符层损坏→抛(在 try 外)
    try:
        registry = LibraryRegistry.model_validate(payload)
    except Exception:
        registry = LibraryRegistry(kind=kind, items=[], ...)   # ← 损坏/schema 不符 被吞成空
    if registry.kind != kind or not registry.items:            # ← 空(损坏 OR 真空)同一分支
        refresh = self.refresh_entries(kind, force=False)      # → _write_registry(全盘重扫)覆盖磁盘
        ...
```

**掩盖的 bug（数据丢失）**：合法 JSON 但 schema 不符的注册表 → `model_validate` 失败被吞成
空 registry → `not registry.items` 为真 → `refresh_entries` 按磁盘源根**全盘重扫并 `_write_registry`
覆盖**。若某些条目的源目录此刻不可达（源根临时离线、手注册条目的源已移走），重扫结果里
就没有它们 → **既有条目被静默抹除**。同理 `_add_or_replace_entry`（被 install/register 三处
调用，`:123/167/350` 一带的 `_write_registry`）先 `_load_registry_items`（损坏→`[]`）再追加新条目
写回 → **用"仅新条目"覆盖整张表**。

**与 §4.1 的区别**：§4.1 是**展示层**误报（capability 检查吐假 not found，只读不写，已于 #4 修复）；
§4.1b 是**存储层**覆盖写（损坏读触发破坏性写，违反"reads must not write"+"区分正常空/出错空"）。
两者**根因不同、修法不同**，不应同 PR。

**修复方向（留给本条目，排在 #4 之后）**：`_ensure_registry` 对"文件存在但损坏/schema 不符"
应 **raise 或走显式 repair 分支**（备份损坏文件 + 重建并告警），而非静默变空触发覆盖；
`_load_registry_items` 应区分"文件缺失（合法 bootstrap，返回 `[]`）"与"存在但不可解析（raise）"；
并核查 `refresh_entries`/`_build_*_entries` 在损坏输入下的行为、补 install/register 流程测试。
**风险更高**（动整条 scan-rebuild 链 + 三处写入点），需单独排期、单独评审。

#### ✅ 已修复（§4.1b，2026-06-22）

`library_registry_service` 把"文件缺失（合法空）"与"文件存在但不可解析（出错空）"彻底分开，
损坏输入不再静默变空、不再触发覆盖写。核心改动（保持 surgical，复用现有 pattern）：

- **新增 `LibraryRegistryError(RuntimeError)` + 模块 `logger`**（此前该文件无 logger，沿用
  `package_service` #4 的 `logging.getLogger(__name__)` pattern）。异常类是"出错空"的显式信号，
  让调用方能区分它与合法空。
- **`_load_registry_items`（写回/读取路径）**：`文件缺失 → []`（合法 bootstrap），
  `文件存在但 char 损坏/schema 不符/kind 不符 → raise LibraryRegistryError`。**绝不再静默 `[]`**——
  这正是 `_add_or_replace_entry`（install/register 三处写入点）此前用"仅新条目"覆盖整张表的根因。
  现在损坏时它 raise、**不写**，损坏文件原样保留（不被破坏性覆盖），靠下方 `_ensure_registry`
  的 quarantine+rebuild 在下次读时自愈。
- **`_ensure_registry`（读路径）走显式 repair 分支**：文件存在但不可解析（含 kind 不符）→
  `_quarantine_corrupt_registry`（把损坏文件改名为 `*.corrupt-<ts>-<uuid8>.bak` 备份 +
  `logger.error` 结构化告警）+ 重建。**关键是把"损坏"与"合法空"在 `not items` 判定之前分开**：
  合法的"文件存在但 items 空"仍走重扫（首次 bootstrap 语义不变），唯独损坏先备份再重建。
  备份名带 timestamp+uuid8，杜绝覆盖既有备份；rename 竞态/失败被 catch 并告警（重建仍会兜底）。
- **`refresh_entries`/`_build_*_entries` 在损坏输入下的行为**（核查结论）：二者读旧注册表仅作
  **摘要缓存**复用。新增 `_load_cached_entries`（best-effort：损坏 → 缓存未命中 `[]` + warning，
  **不 raise**），`_build_skill_entries`/`_build_mcp_entries` 改用它。这样 force-refresh 在损坏
  注册表上仍能从磁盘源**恢复**（重扫覆盖损坏），而不是被解析错击穿——既满足"出错不静默"，
  又保留 refresh 作为显式重建/修复手段的语义。`resummarize_entry` 因先调 `_ensure_registry`
  （已自愈）再在锁内 `_load_registry_items`，路径同样安全。

**新增测试** `backend/tests/test_library_registry.py::TestLibraryRegistryCorruptionSafety`（9 例，
隔离 `skill_roots=[]/mcp_roots=[]` 避免 home 目录技能污染）：(a) 文件缺失→`[]`/`_ensure_registry`
合法空且不 quarantine；(b) char 损坏/schema 不符/kind 不符→raise；(c) 损坏读 quarantine+rebuild
不抹既有条目、备份含原始损坏字节、live 文件已重建、多条目损坏后全部恢复、install 在损坏注册表上
raise 且**不覆盖**损坏文件（断言文件字节未变）；(d) force-refresh 从损坏 previous 恢复。

**全量回归**：`PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests`
**569 全绿**（含原 16 例 install/register 安全测试与 `test_package_capabilities` #4 用例，均不回归）。

**与修复原则对应**：满足"区分正常空与出错空"（缺失 vs 不可解析）、"读操作不写"的精神
（损坏读改为 quarantine 独立备份 + 告警，不在正常读路径静默改持久化表）、"出错显式而非空集"。

### 4.2 [HIGH] executor profile 解析失败 → 编造假 profile

`backend/app/workers/agent_cli_executor.py:982-1036`

四层叠加的吞异常：profile 解析失败 `pass` → 默认 profile 加载失败 `pass` → 编造一个
`ExecutorProfileSpec(profile_id=f"{provider}-{auth_mode}")` → settings 加载失败 `pass` 传
`None` 给 renderer → renderer 失败只 `print()` 到 stdout（无 logger）。

**掩盖的 bug**：profile 配错被层层吞，run 用一个**猜测的 profile_id 和 None settings**
启动，用户看到的是通用执行失败，而非"你的 profile 坏了"。

#### ✅ 已修复（#5，2026-06-22）

`agent_cli_executor._try_render_provider`（子进程 wrapper，无 logger）改为「区分错-空/真-空 +
复用既有结构化失败通道」，不再层层吞异常后编造假 profile / 静默回落 template。

- **复用既有终态通道，而非新建**：executor 把 `executor_failure.json`（`ExecutorFailureReport`）
  + `terminal_report.json`（`TerminalReport`, `terminal_kind="synthetic_failure"`,
  `reason_code`+`summary`）写进 run_dir。`worker_service:1198-1210` **优先**读它并以具体 summary
  呈现失败，**先于**通用「退出码 N」分支（:1251）。这正是 command_worker 已用的契约；新增一个
  本地小 writer `_write_setup_failure` + 异常 `_SetupFailure`（在唯一调用点 main() 捕获 → 返回退出码 2）。
- **层 1（stored profile 解析）**：`except: pass` → `except Exception as exc:`。**显式请求的
  profile_id** 解析抛异常 = 硬 config 错（与 adapter 层 `_resolve_profile_hints:148` 的 raise 一致）
  → 写结构化失败「Executor profile '<id>' could not be loaded: <exc>」并终止，**不编造**。无显式
  profile_id 时记录并继续走 default/fabricate（无具体 profile 可失败，保留兜底）。
- **层 2（default 查找）**：`except: pass` → 记录异常后继续 fabricate（`default_profiles()` 是内建，
  抛异常记日志但不独立致命）。
- **层 3（编造）**：**真-空**（无 stored、无 default 匹配、无 error）仍 fabricate 最小 spec ——
  这是 `cli_native` 的合法兜底（renderer 测试证最小 spec 够用），保留。
- **层 4（settings 加载）**：`except: pass` 传 `None` → 改为抛异常即写结构化失败「Failed to load
  settings」并终止（settings 缺失下游必然坏）。
- **层 5（renderer.render 抛异常）**：原仅 `print()` 后返回 None → 静默回落 legacy template
  （根因黑洞）。改为写结构化失败「Provider renderer failed for <provider>: <exc>」并终止。
  `renderer is None`（无渲染器注册）仍合法回落 template，二者区分开。
- `reason_code` 枚举（`app/models/runs.py:12`）无 `configuration_error`，统一用 `execution_error`，
  由 summary 承载具体原因；`details.phase` 标注 `profile_resolution`/`settings_load`/`render`。
- **测试**：`backend/tests/test_executor_setup_failure.py`（6 例，此前零覆盖）——显式 profile 加载错→
  结构化失败+不调 render、无 profile_id 加载错→回落 fabricate、render 抛异常→结构化失败、settings 抛
  异常→结构化失败、真-空→fabricate 且渲染、无渲染器→优雅返回 None。读回 terminal_report/executor_failure
  以真模型校验。
- 全量 541 测试中 540 通过，新增 6 例全绿。剩 1 个 pre-existing 失败
  （`test_clearing_default_provider_key_clears_legacy_secret`，`clear_api_provider_keys` 清 deepseek
  后被 `BLUEPRINT_DEEPSEEK_API_KEY` env fallback 复活）与 #5 无关，在 clean tree 同样失败，已单开待办。

**采纳的产品决策**（用户 2026-06-22 拍板，均选推荐项）：renderer.render() 抛异常 = 写结构化失败并
**终止**（不回落 template，即便有 template）；显式请求的 profile 解析抛异常 = 写结构化失败、**不编造**。
风险评估：profile-error 路径今天吞异常后 fabricate→render 下游几乎必然失败，A1 只改善信息不会把成功变
失败；render-throw-终止在「provider 同时有 renderer 和可用 template」时会把今天靠回落跑通的 run 变失败，
但 renderer 存在却抛异常时回落 template 几乎必是错的，故接受。

### 4.3 [HIGH] 脱敏/泛化失败 → 原始蓝图原样发布

`backend/app/services/card_desensitization_service.py:97-99` + `card_library_service.py:711-715`

```python
except Exception:
    # Best-effort: never propagate to the review pipeline.
    return None
```

**掩盖的 bug**：脱敏/泛化的任何失败（key 错、DeepSeek 500、tool JSON 畸形、ValidationError）
都塌缩成 `reason="unavailable"`，**原始未脱敏蓝图原样发布**。若 agent 输出有 schema 校验
bug，泛化永远静默跳过，发布流水线仍报成功。

### 4.4 [HIGH] manifest 自动补丁失败 = "无需补丁"

`backend/app/services/manifest_service.py:350-359`

```python
except Exception:
    return False   # ← 损坏的 manifest 和"本来就没问题"无法区分
```

**掩盖的 bug**：`attempt_auto_patch_manifest` 返回 `False` 既能表示"manifest 没问题"也能
表示"manifest 损坏、放弃了"。调用方无法区分。损坏的 manifest 静默跳过资产对账，下游用
残缺 manifest 继续。

### 4.5 [HIGH] 诊断数据加载失败 → 诊断报告"一切正常"

`backend/app/services/worker_service.py:2442-2450`

```python
try:
    runs = ...load_runs()
    active = [run for run in runs if run.status in active_statuses]
except Exception:
    active = []   # ← 加载失败 = "无活跃 run"
```

**掩盖的 bug**：runs 加载失败时 `active=[]`，后续 `stuck_ids` 计算也基于空列表 → 诊断包
报告"无卡住的 run"，而真实情况是 run 都加载不出来。运维查卡死执行器，拿到的是假"健康"
诊断。

### 4.6 [MED] 工作板/后台任务 JSON 损坏 → 静默清空

`background_workboard_service.py:1176-1177`（损坏 → 空状态，UI 显示"无待办"）、
`background_task_service.py:102-105`（逐条 `model_validate` 失败的 `continue`，任务从 UI
消失）、`manager_auto_service.py:82-83`（某项目 graph 损坏 → `continue` 跳过，该项目
`wake_in_flight` 永远卡 True）。

**共同掩盖的 bug**：JSON 损坏表现为"功能空了"而非"数据坏了"。损坏记录留在磁盘反复被
丢弃，从不修复或告警。

### 4.7 [MED] reference_usage 跳过解析失败的蓝图 → 误判"未被引用"

`card_library_service.py:311-314`

```python
try:
    bp = CardBlueprint.model_validate(read_json(bp_path, {}))
except Exception:
    continue   # ← 解析失败的蓝图被跳过 = 视作"不引用该资产"
```

**掩盖的 bug**：删除引用资产前检查"有无蓝图引用它"。解析失败的蓝图被跳过 →
`reference_usage` 返回 `[]` → 误删被引用的资产 → 破坏蓝图。

---

## 五、硬编码魔法值（配置漂移与一致性问题）

### 5.1 [HIGH] 状态集合字面量散落 86 处，已有确定分歧

- 活跃 run 状态集：`worker_service.py:2415` `_active_run_statuses()` 返回
  `{"queued","launching","needs_approval","running","reviewing"}`（5 个，基准），
  在多处重复，且多处缺项——核查确认的分歧：

  | 位置 | 实际集合 | 缺少 | 影响 |
  |------|----------|------|------|
  | `worker_service.py:3154`（reconcile） | `{"queued","running","reviewing"}` | `launching` | **确定 bug**：`launching` 的 run 已 `subprocess.Popen`（有进程、有线程），重启后进程孤儿、线程句柄丢失，reconcile 跳过它 → 既不标 `failed` 也不清理，成幽灵活跃 run。**注意：`needs_approval` 不在此列**——它是 run 创建时的纯等待态（等用户批准，未起进程无线程），reconcile 本就不该触碰；用户重启后继续审批即可从磁盘恢复。**若误把 `needs_approval` 加入 reconcile 活跃集，会误杀所有暂停等待批准的 run** |
  | `manager_blueprint_tools.py:331` | `{"queued","running","reviewing","needs_approval"}` | `launching` | `launching` 阶段的 run 不算活跃 |
  | `manager_blueprint_tools.py:3928`（`_compact_card`） | `{"queued","running","reviewing","needs_approval"}` | `launching` | 同上 |
  | `background_workboard_service.py:25` `_ACTIVE_TASK_STATUSES` | `{"queued","launching","running","waiting"}` | — | **维护风险，非现存 bug**：此集合管的是**后台任务状态域**（含 `waiting`，无 `reviewing`/`needs_approval`），与 run 状态域是不同概念，但命名 `_ACTIVE_TASK_STATUSES` 易与 `_active_run_statuses()` 混淆，未来易踩坑 |

  其中 `worker_service.py:3154` 的 reconcile 漏 `launching` **是确定的现存 bug**——重启后
  `launching` 的 run 漏网，不被标 failed、不被清理，永久占位（它的进程已孤儿但状态仍是
  launching）。`needs_approval` **不属于这个 bug**，它是纯等待态，不该被 reconcile 处理。

- 终态集 `{"success","failed","cancelled","reviewed"}`：`manager_blueprint_tools.py:2728`
  用 `{"reviewed","cancelled"}`、`:2762` 用 `{"failed","cancelled","reviewed"}`——一个少
  `success`、一个少 `failed`，不一致。
- `status_rank` 字典 `{"valid":0,"candidate":1,"stale":2,...}` 在 3 个文件逐字复制；
  `{"valid","candidate"}` 输入状态集在 4+ 文件用 3 个不同常量名。

**掩盖的 bug**：新增状态时，漏改某个副本 → 部分代码用旧集合，状态判定局部错误。
`worker_service.py:3154` 漏 `launching` 是现存 bug 的直接证据；其余分歧行为虽未爆发，但
字面量散布本身是维护陷阱。

**修复方向警告**：集中常量时，reconcile 的活跃集**不能**简单照搬 `_active_run_statuses()`
的 5 元全集，也不能按"有无线程"一刀切。正确判据是 **run 状态的磁盘可恢复性**：

- `needs_approval`（纯等待态，未起进程无线程，用户重启后继续审批即可从磁盘恢复）→ **唯一**
  应排除在 reconcile 之外的非终态。误加入会重启误杀所有"正等批准"的暂停 run。
- `queued`（`start_run` 抢 slot 后创建、同调用内 `thread.start()`，转瞬即逝，无 pump/无重调度）
  → 重启后无线程的 queued run 是**孤儿**，**应被 reconcile 处理**（标 failed）。上一版审计
  误称"未调度的 queued 应排除"是错的——见 §2.3 二次修正。
- `launching`/`running`/`reviewing`（有进程或执行中）→ 应被 reconcile 处理（标 failed）。

**结论**：reconcile 应处理集 = `_active_run_statuses()` 减去 `needs_approval` =
`{queued, launching, running, reviewing}`。详见 §2.3 的状态对照表与判断框架。

**#3 实施记录（2026-06-22）**：已落地。新增单一真源 `app/models/graph.py` 6 常量
（`ACTIVE_RUN_STATUSES` / `RESTART_ORPHANED_RUN_STATUSES` / `TERMINAL_RUN_STATUSES` /
`VALID_INPUT_ASSET_STATUSES` / `ASSET_STATUS_RANK`），跨 13 文件统一引用；reconcile 改用
`RESTART_ORPHANED_RUN_STATUSES`（净变化＝新增 `launching`，`needs_approval` 明确排除）；
新增 `backend/tests/test_reconcile_active_runs.py`（4 例）覆盖重启 orphan→failed /
needs_approval 保留 / terminal 不动 / card 同步 failed。全量套件 529 passed。

> **遗留决策 · `_active_run_statuses()` 薄包装**：该静态方法现仅 `return ACTIVE_RUN_STATUSES`。
> **有意保留**——它是 7 处调用点的语义化稳定接口，且为未来"按上下文派生活跃集"预留扩展点；
> 删除需改 7 处、收益不大。**不属于 #3 范围**。若 #6（2.1）/#7（2.3）改状态机时顺手收口可一并清理，
> 否则维持现状。

### 5.2 [HIGH] `"__system__"` 哨兵散落 6 文件无常量

`worker_service.py`、`card_library_service.py`、`project_service.py`、
`manager_blueprint_tools.py`、`package_service.py`、`runtime_dependency_resolver_service.py`
各自写字面量 `"__system__"`。任一处拼错（`"__system"` / `"system"`）静默改变 runtime 解析。

### 5.3 [HIGH] 保留环境名检查硬编码

`manager_blueprint_tools.py:1361`

```python
if env_name in {"base", "__system__"} or env_name.startswith("blueprint-re-"):
```

`"blueprint-re-"` 前缀是唯一强制点。命名方案一变，`create_runtime` 允许用户创建冲突名。

### 5.4 [MED] bundled mamba 路径两处来源

`project_service.py:1088` `Path.home()/".local/share/blueprint-re/mamba"` 与
`config.py:32` 重复。一处改、一处不改 → env 被误判为 `"system"` 来源。

### 5.5 [MED] omicverse MCP 入口点硬编码

`library_registry_service.py:568-587,1204-1214` 内联 `"args": ["-m","omicverse.mcp","--phase","P0"]`。
omicverse 改 MCP entrypoint 即坏，无配置覆盖。

### 5.6 [MED] 超时/重试/限制硬编码，忽略已有 config 旋钮

- `manager_blueprint_tools.py:3535,3558` `timeout=60`、`library_registry_service.py:739`
  `urlopen(timeout=60)`——但 `config.py:163` 有 `runtime_dependency_probe_timeout_seconds`，
  这些路径不读它，旋钮形同虚设。
- `pi_agent_executor.py:306` `range(1,6)` 硬编码 5 次重试，与 `manager_planner.py:279`
  `retry_provider_call(max_attempts=5)` 是同一逻辑的两套策略。
- `chat_session_service.py:169` 与 `project_event_service.py:30` 各自 `queue.get(timeout=15)`，
  心跳节拍两处独立。

### 5.7 [MED] 错误上报一律写 "deepseek"

`manager_planner.py:269`、`executor_reviewer_worker.py:291`、`card_desensitization_service.py:182`、
`manager_service.py:130` 都 `provider="deepseek"` 字面量传给错误报告。实际调用走了
Anthropic/OpenAI 时，错误报告仍说 deepseek，误导运维。

### 5.8 [LOW] 诊断里 getattr 死默认值

`diagnostic_bundle_service.py:392` `getattr(self.settings, "pi_manager_url", "http://127.0.0.1:18002")`
——`pi_manager_url` 是 Settings 必有字段，getattr 默认值是死代码；属性一旦改名，探测静默
指向错误 host。

---

## 六、`getattr` 兜底本应存在的字段（掩盖模型校验失败）

### 6.1 [HIGH] Card/Module 必有字段用 getattr 兜空

`backend/app/services/project_file_service.py:265-271`

```python
active_refs.update(getattr(card, "linked_assets", []) or [])
active_refs.update(item.asset_id for item in getattr(card, "inputs", []) or [] ...)
```

`linked_assets`/`inputs`/`outputs` 是 Card 声明字段，`depends_on_assets` 是 Module 字段。
`getattr(..., [])` 兜底 → 校验失败的畸形卡片被当作"无输入输出" → 被分类为"无引用" →
可能被当垃圾回收清除出活跃集。

### 6.2 [HIGH] resolver 未初始化时默认"允许安装"策略

`runtime_dependency_resolver_service.py:896,965,1071,1257`

```python
policy = getattr(self, "_active_policy", "allow_safe_registry_install")
```

`_active_policy` 在 `__init__` 设置。getattr 默认值是**宽松策略**——若属性因 bug 未设置，
resolver 静默允许 registry fallback 安装，而非 fail-closed。安全策略默认开，方向错了。

**实现记录（#9，2026-06-22）**：

核查后修正了 finding 的细节：`_active_policy` 此前**并非**在 `__init__` 设置，而是仅在
`resolve()`（line 404）设置；4 处读取点的 getattr 默认值还**互相不一致**——

| 行 | 用途 | 原默认值 | 方向 |
|---|---|---|---|
| 896 | solver_error 降级 fallback 安装的开关 | `"report_only"` | fail-closed |
| 965 | `collect_fallback_actions` 的策略门 | `"allow_safe_registry_install"` | **fail-open** |
| 1071 | fallback 状态判定 | `"allow_safe_registry_install"` | **fail-open** |
| 1257 | 缓存键组件 | `"report_only"` | fail-closed |

修法（保持 surgical）：

1. `__init__` 建立 fail-closed 实例不变量 `self._active_policy: str = "report_only"`——属性
   恒有定义，且在 `resolve()` 归一化覆盖之前是**限制性**策略，未初始化绝不会静默授权安装。
2. 4 处读取点从 `getattr(self, "_active_policy", <varies>)` 改为直接 `self._active_policy`，
   消除默认值不一致。
3. `resolve()`（line 404）仍 `self._active_policy = normalize_fallback_policy(policy)`——正常
   路径行为不变（resolve 永远先于这些 helper 设置策略）。
4. +2 回归测试：`test_active_policy_defaults_fail_closed_before_resolve`（构造即 `report_only`）、
   `test_active_policy_reflects_requested_policy_after_resolve`（resolve 后反映请求策略）。
   现存 `test_solver_error_prevents_fallback`（report_only 下 solver_error 不自动 fallback）佐证方向。

> **KI-2（范围外观察，待用户裁决）**：`normalize_fallback_policy("unknown")` 返回
> `"allow_safe_registry_install"`（line 1733，**fail-open**），且被 `test_normalize_policy`
> line 529 `assertEqual(normalize_fallback_policy("unknown"), "allow_safe_registry_install")`
> 固化为**有意测试行为**。这与 §6.2「安全策略应 fail-closed」方向相反，但归一化未知字符串
> 为宽松是另一处独立决策（影响面：配置/UI 传入非法策略字符串时的兜底方向）。**不在 §6.2
> 范围，本次未改动**；若要改为未知→`report_only` fail-closed，需同步改测试 line 529，请单独
> 裁决后再动。

### 6.3 [MED] executor_context 用 getattr 兜底

`worker_service.py:2063-2066`

```python
getattr(getattr(card, "executor_context", None), "script_preference", None)
```

`executor_context` 是卡片必有字段。缺失（构建 bug）时静默回退图级偏好继续跑，而非报错。

---

## 七、`or` 链合并多数据源（产生不自洽的记录）

### 7.1 [HIGH] payload ⊕ result 合并可产出不自洽记录

`runtime_dependency_state_service.py:146-150`、`background_workboard_service.py:983-990`

```python
runtime = str(source.get("runtime") or result.get("runtime") or "unknown")
ecosystem = str(source.get("ecosystem") or result.get("ecosystem") or "unknown")
```

**掩盖的 bug**：`source`（计划）和 `result`（实际）描述不同 job 时，首个非空值胜出，
产出"runtime=python（来自 source）、packages=[R 包]（来自 result）"的不自洽记录并持久化。
工作板显示与实际运行不符的 runtime。

### 7.2 [MED] 时间戳 created→started→finished 兜底

`runtime_dependency_state_service.py:250`

```python
created_at = str(item.get("created_at") or item.get("started_at") or item.get("finished_at") or "")
```

缺 `created_at` 时用 `finished_at` 冒充 → 时间线排序错误。

### 7.3 [MED] tool_call id 三级兜底

`chat_stream_relay.py:250,265,282`

```python
item_id = str(payload.get("tool_call_id") or self._last_matching_tool_id(...) or self._timeline_item_id(...))
```

无 `tool_call_id` + 歧义 `tool_name` → 匹配到错误的 `tool_start` 或生成重复 timeline 项。

---

## 八、`# noqa` / `# type: ignore` 压制真问题

| file:line | 压制 | 风险 |
|---|---|---|
| `runtime_dependency_resolver_service.py:1170` | `# noqa: BLE001` | **最差**——主动压制本次审计针对的 broad-except 规则，且注释 `# the resolver is best-effort` 把"掩盖 bug"正当化 |
| `runtime_dependency_resolver_service.py:554` | `# type: ignore[attr-defined]` | 访问 `plan._candidate_actions` 私有属性；字段改名 → 运行时 AttributeError |
| `worker_service.py:1521` | `# type: ignore[attr-defined]` | `return sem._value` 读 CPython 私有；Py3.13+ 内部变动即坏 |

---

## 修复优先级建议

按"掩盖的 bug 危害 × 用户触达频率"排序，**先修这 10 个**（外加 §4.1b 追加 HIGH 收尾）：

1. **1.3** `patch_apply.py:319` — 半恢复 graph 损坏 + 非原子写入（数据丢失级，补测试）✅ 已完成
2. **1.1** `bootstrap_from_aliases` 读改写 + 启发式绑定（与 docs/66 依赖链直接相关）✅ 已完成
3. **5.1** 状态集合字面量集中化（含 `worker_service.py:3154` reconcile 漏 `launching` 的现存 bug；**注意 `needs_approval` 必须排除在 reconcile 外，见 §2.3 纠错**）✅ 已完成
4. **4.1** 注册表失败 → 全员"技能缺失"（用户高频踩到的假阳性）✅ 已完成（仅 site 1 展示层；存储层覆盖写另立 §4.1b）
5. **4.2** executor profile 编造 + stdout-only 错误（执行器失败的根因黑洞）✅ 已完成
6. **2.1** plan revision coerce planned（状态机被绕过）✅ 已完成（重置语义保留，coerce 改为显式 `plan_revision_warnings` + manager_review append；running/reviewing 不对称留 §2.x 状态机）
7. **2.3** 重启强标 run failed（误杀正常 run）✅ 已完成（核心状态集缺口由 §5.1 交付；#7 改盲写 summary 为读盘保真 `_reconcile_run_summary`：report_fail/synthetic 保留真因、report_complete 诚实重跑文案、孤儿保留通用文案；删 `thread.is_alive()` 死代码；card.manager_review 改 append；+6 测试。成功恢复另立 §2.3b）
8. **3.1** 全角色默认 deepseek（配置错误表现为功能 bug，Python + JS 两侧）✅ 已完成（不改产品默认 deepseek；集中 `DEFAULT_PROVIDER_ID` 常量 Python+JS 双侧；删 `manager_agent_config:231` 的 `or "deepseek"` 死掩盖；KI-1 按用户裁决用测试隔离 env，保留 env 合法配置层；+1 回归测试）
9. **6.2** resolver 默认宽松策略（安全方向错误）✅ 已完成（4 处 `getattr(self, "_active_policy", …)` 读取点默认值不一致——896/1257 fail-closed、965/1071 fail-open；统一改为 `__init__` 建立 fail-closed 实例不变量 `self._active_policy = "report_only"`，4 处读取点直接读 `self._active_policy`；resolve() 仍按请求归一化覆盖；+2 回归测试锁定"未 resolve 即 fail-closed"+"resolve 后反映请求策略"。**范围外观察**：`normalize_fallback_policy("unknown")→allow` 是 fail-open，但被现存测试 line 529 固化为有意行为，不在 §6.2 范围，未改动，留作单独决策项见下方 KI-2）
10. **3.4 + 3.5** CORS 端口 + key 空串兜底（配置与行为脱钩）✅ 已处置（无需改码）。**§3.4**：bug 属实但被 docs/64 §1.4 显式认领（与 nginx 端口模板化 + 部署脚本端口渲染强耦合），用户裁决延期随 docs/64 一并修。**§3.5**：核查发现审计核心指控「Anthropic/OpenAI 跳过 settings 层」已过期——三个 provider key 现均为 config→settings→env 三层，唯 Tavily 有意 env-only 且可选；`缺失→""` 是可选 key 的合法禁用信号，必需的 DeepSeek 已在用例点强制（在 `_effective_deepseek_api_key` 内改 raise 反会击穿 `get_public_settings` 状态展示）。用户裁决关闭，仅留处置记录
11. **4.1b** `_ensure_registry`/`_load_registry_items` 损坏输入触发全盘覆盖写（数据丢失，存储层）✅ 已完成（追加 HIGH 收尾，2026-06-22；区分缺失→`[]` vs 损坏→raise/quarantine 备份+重建，写回路径不再用空覆盖，refresh 缓存读容错可从损坏 previous 恢复；+9 测试，569 全绿。详见 §4.1b 实施记录）

> **追加 HIGH（核查中发现，排在主序之后）**：
> - **§2.3b** 重启后不重放 finalization：`report_complete` 孤儿当前诚实标 failed 提示重跑（保真但非恢复），成功恢复（启动重放 finalize 链）高风险，单独立项（#7 拆分）。

### 排期顺序说明

5.1（状态集合集中化）排在 #3 而非原 #8——原因：修 2.1/2.3/2.2 时必然会触碰状态集合，
若不先把常量集中，新修的代码又会引入一份新的字面量副本。**先集中常量，再修状态机逻辑，
事半功倍**（采纳审阅意见）。同理 1.3/1.1 在前，因为 docs/66 依赖链重设计要建立在资产绑定
可信之上。

### 修复原则（避免引入新问题）

- **区分"正常空"与"出错空"**：出错时返回显式错误结构（如 `Result(ok=False, error=...)`），
  而非空集合/None/False。调用方必须处理 error 分支。
- **读操作不写**：bootstrap/修复逻辑若必须存在，应写独立 repair 文件 + 告警，不污染正常
  读路径，不静默改持久化状态。
- **状态 coerce 改为显式校验**：非法状态应报错或走显式"重置"动作，不静默 coerce。
- **集中常量**：状态集、哨兵、保留名抽到单一模块，消除 86 处字面量。
- **配置缺失即报错**：像 `BLUEPRINT_INTERNAL_TOOL_TOKEN` 那样，缺失抛错而非空串兜底。

### 与其他文档的关系

- docs/66 的依赖链重设计依赖本文 1.1（资产绑定不可信）被修复——否则失效传播建立在
  不可信的绑定之上。
- docs/64 的端口模板化已覆盖 3.4（CORS）和部分 5.x（端口硬编码）。
- 本文是审计快照，修复时需逐条核实行号（代码可能已变动），以实际代码为准。

## 已知既有失败（跟踪用，非本轮回归）

修复 #1（1.3）/#2（1.1）期间发现的、**与本轮改动无关**的既有问题，记录在此避免遗忘。
不在本轮修复，留给对应条目处理。

### KI-1 · `test_clearing_default_provider_key_clears_legacy_secret`（env 耦合，属 #8 / §3.1+§3.5）

- **现象**：`backend/tests/test_executor_profiles.py::TestExecutorProfileResolution.
  test_clearing_default_provider_key_clears_legacy_secret` 在**已 export provider key 的环境**
  （如 dev shell 设了 `BLUEPRINT_DEEPSEEK_API_KEY`）下失败，最后一行
  `self.assertIsNone(settings.deepseek_api_key)` 不成立。在所有 provider env 均 unset 的
  干净环境下通过。
- **根因**：`config.py` 的 `deepseek_api_key: SecretStr | None` 是 **env 兜底字段**。
  `clear_api_provider_keys` 清掉 settings 实例上的值后，`get_settings()` 读取仍被环境变量
  遮蔽 → 测试期望的 `None` 被 env 值覆盖。这与 §3.5「key 空串/env 静默兜底」、§3.1「provider
  默认值与真实配置脱钩」同根。
- **定性**：用 `git stash` 在基线（无 #1/#2 改动）上复现确认——**既有 bug，非本轮回归**。
  #1/#2 的 diff 仅触及 patch_apply / utils / asset_materialization / project_service(materialization)
  / worker_service(materialization 日志)，**零 provider-config 代码**。
- **处置**：留给 **#8（3.1，provider key env 兜底）**。修 §3.1 时一并让 `clear_api_provider_keys`
  对 env 遮蔽做显式处理（清除即清除，不被 env 静默回填，或测试显式隔离 env），并去掉静默兜底。
