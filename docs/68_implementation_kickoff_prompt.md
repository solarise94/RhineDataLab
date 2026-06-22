# 项目实施启动 Prompt

> 用法：把下面 ``` 包裹的整段，粘贴给新的 LLM 会话（Codex / 其他 agent）作为第一轮输入。
> 假设 agent 已在仓库根目录 `/home/solarise/blueprint_re_v3`（或对应 checkout），有读写权限。

---

````
你是这个仓库（blueprint_re_v3）的实施 agent。下面是你的工作起点。

## 0. 先读这些文档，按顺序

开工前必须读完，它们是决策依据（README 和 docs/00 的定位表述已过时，以这些为准）：

1. docs/65_product_positioning_workflow_ide.md  —— 产品定位锚（工作流 IDE，不是自动科研平台）
2. docs/67_silent_fallback_and_hardcode_audit.md —— 你这一轮要修的审计清单
3. docs/66_execution_model_and_dependency_chain_redesign.md —— 依赖链重设计（你的修复要为它铺路，但不实现它）
4. AGENTS.md —— 架构规则、路径/隐私规则、编码风格、构建与验证命令

读完 65 之前不要写任何代码。如果发现某处行为与 65 冲突，以 65 为准，并在 PR 里说明冲突点。

## 1. 这一轮的目标

按 docs/67 §"修复优先级建议"的顺序，逐条修复审计发现。**只做 docs/67，不碰 docs/64（打包）/docs/66（依赖链）的实现**——那是后续轮次。但你的修复不能挡路：修 1.1（资产绑定可信）时要考虑 docs/66 会依赖它；修状态集合（5.1）时要考虑 docs/66 的 stale 传播会复用这些常量。

按 docs/67 调整后的顺序执行（务必遵守，顺序有依赖关系）：
  #1  1.3  patch_apply.py:319 半恢复 graph + 非原子写入（补测试）
  #2  1.1  bootstrap_from_aliases 读改写 + 启发式绑定
  #3  5.1  状态集合字面量集中化（含 worker_service.py:3154 reconcile 漏 launching 的现存 bug）
       ⚠️ reconcile 陷阱：集中常量时，reconcile 的活跃集**绝不能**机械照搬
       `_active_run_statuses()` 的 5 元全集，也不能按"有无线程"一刀切。正确判据是
       **run 状态的磁盘可恢复性**：
       - `needs_approval`（纯等待态，未起进程无线程，用户重启后继续审批即可从磁盘恢复）
         → **唯一**该排除在 reconcile 之外的非终态。误加入会重启误杀所有"正等批准"的暂停 run。
       - `queued`（start_run 抢 slot 后创建、同调用内 thread.start()，转瞬即逝，无 pump/
         无重调度）→ 重启后无线程的 queued run 是**孤儿**，**应被 reconcile 处理**（标 failed）。
         ⚠️ 上一版审计误称"未调度的 queued 应排除"是错的，见 docs/67 §2.3 二次修正。
       - `launching`/`running`/`reviewing`（有进程或执行中）→ 应被 reconcile 处理（标 failed）。
       结论：reconcile 处理集 = `_active_run_statuses()` 减去 `needs_approval`
       = {queued, launching, running, reviewing}。详见 docs/67 §2.3。
  #4  4.1  注册表失败 → 全员"技能缺失"
  #5  4.2  executor profile 编造 + stdout-only 错误
  #6  2.1  plan revision coerce planned
  #7  2.3  重启强标 run failed
  #8  3.1  全角色默认 deepseek（Python + JS 两侧）
  #9  6.2  resolver 默认宽松策略
  #10 3.4+3.5  CORS 端口 + key 空串兜底

## 2. 不可违反的修复原则（docs/67 §修复原则 的硬约束）

- 区分"正常空"与"出错空"：出错返回显式错误结构（如 Result(ok=False, error=...)），不要返回空集合/None/False 让调用方分不清。调用方必须处理 error 分支。
- 读操作不写：bootstrap/修复逻辑若必须存在，写独立 repair 文件 + 告警，不污染正常读路径，不静默改持久化状态。
- 状态 coerce 改为显式校验：非法状态报错或走显式"重置"动作，不静默 coerce 成合法状态。
- 集中常量：状态集、哨兵、保留名抽到单一模块（建议 backend/app/core/constants.py 或 models 里），消除散落的字面量副本。**修状态机逻辑前必须先做这一步，否则会引入新副本。**
- 配置缺失即报错：像 BLUEPRINT_INTERNAL_TOOL_TOKEN 那样，缺失抛错，不要用空串/默认值兜底然后静默 401。
- 跨语言一致：3.1（provider 默认）要 Python（app_config_service.py）+ JS（manager-agent/src/server.js:15）两侧一起改。

## 3. 第一步：只做 #1（1.3 patch_apply.py:319），做成示范

不要一次性把 10 条全做完。先用 #1 验证你的工作流和我的验收标准对齐：

文件：backend/app/services/patch_apply.py，函数 _restore_snapshot（约 300-325 行）。
问题：第 319 行 path.write_bytes(payload) 非原子写入，且外层 except Exception: return False 吞掉异常。
要做：
  a. write_bytes 换成项目自有的 atomic_write_json（backend/app/services/utils.py:15，同文件已 import）。注意 atomic_write_json 输入是 object（会 json.dump），而这里 payload 可能是已序列化的 bytes——先确认 payload 类型，若已是 bytes 则用等价的 tempfile+os.replace 原子写入，不要破坏数据。
  b. except 里加 logger.exception 记录具体异常；返回值要能让调用方区分"无需恢复"与"恢复失败"（看 184-187 行调用方怎么用返回值，设计一个不破坏现有契约的方案，比如返回 None 表示"无需恢复"、False 表示"恢复失败"，或在返回 False 时附带 error 字段——但优先看现有契约，选最小破坏方案）。
  c. 在 backend/tests/ 补 _restore_snapshot 的测试（当前零覆盖）。至少覆盖：正常恢复成功；恢复中途模拟写入失败（可 mock os.replace 抛异常）应不留下截断文件、应记日志。
  d. 不要顺手改同一函数里的其他逻辑——保持 diff 聚焦。

## 4. 验证（每一步都要跑）

改完任何后端代码，按 AGENTS.md 的命令验证：
  PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests
如果改了 Pydantic 模型或 patch schema，按 AGENTS.md 要求重新生成 backend/app/schemas/*.json（看 scripts/generate_backend_schemas.py），并在交付前确认 git diff 里 schema 文件已更新。
改了 manager-agent 的 JS（如 #8），跑 cd manager-agent && node --check src/server.js。
#1 不涉及模型/schema 变更，所以只需跑测试。

## 5. 工作方式

- 一次只做一条审计项，做完验证通过再开下一条，在对话里告诉我进度。
- 行号可能已变动，以函数名/代码内容定位，不要死磕行号。
- 遇到 docs/67 描述与代码实况不符时，以代码为准，并指出来——审计是快照，不是真理。
- 不要为了"干净"引入新抽象。优先用现有 pattern（atomic_write_json、read_json、项目已有的 Result/错误返回风格）。看周围代码怎么写就怎么写。
- 遇到需要产品决策的歧义（比如"恢复失败应该返回什么"有多种合理方案），先停下来问我，列 2-3 个选项让我选，不要自己拍板。

现在开始：读 docs/65 和 docs/67，然后开始 #1。开始前用一句话告诉我你对 #1 的实现计划。
```
