# Portable Skill / MCP Card Package 方案

## 目标

这份文档定义一种可迁移的 `card package` 结构，用来承载：

- card 级执行目标；
- skill / MCP 挂载选择；
- Python / R runtime 要求；
- 少量可随包迁移的自定义文件；
- 可实例化为项目内真实 card 的输入输出契约。

目标不是直接搬运某个项目里的 live card，而是提供一种“像发牌一样分发”的可复用方法包，让不同用户可以把同一张方法牌导入各自项目，再完成本地绑定与执行。

本轮是架构方案，不包含代码实现。

## 结论先行

这个方向可行，但前提是明确区分三层对象：

1. `library item`
2. `card package`
3. `project card`

其中：

- `library item` 是系统级能力条目，例如 skill / MCP registry；
- `card package` 是可迁移、可导入、可实例化的方法包；
- `project card` 是某个项目中的具体实例，持有本项目资产引用、状态和运行记录。

如果把当前 live card 直接作为跨用户迁移单位，会立即遇到资产引用失效、运行时不兼容、路径不可移植、认证缺失等问题。因此应新增“portable package”层，而不是直接复用项目内 card JSON。

## 为什么不能直接迁移 live card

当前 card 模型适合项目内运行，不适合跨用户搬运。

主要原因：

1. `inputs` / `outputs` 常带项目内 `asset_id`
2. `linked_runs` / `linked_assets` / `status` 都是项目事实
3. `executor_context.skills` / `mcp_servers` 当前是“引用 registry id”，不是随 card 一起封装的实体
4. skill / MCP registry 中存在主机本地 `source_path`
5. runtime 名称相同，不等于目标用户具备同名环境、二进制、认证和依赖

因此可迁移对象不能是“已绑定实例”，而应是“待绑定模板包”。

## 设计原则

- card package 是方法复用层，不是项目事实层。
- package 可以声明依赖 skill / MCP，但默认只引用，不内嵌复制第三方能力本体。
- package 可以携带少量自定义文件，但这些文件必须是 package-local 资源。
- package 不得携带 secrets、用户私有路径、token、host-specific config。
- package 导入后必须经过一次显式 resolve / bind，才能变成真实 card。
- package 的失败状态应在导入阶段暴露，而不是等到 run 阶段才随机爆炸。

## 与现有结构的关系

仓库现状已经有几块可复用基础：

- `Card.executor_context`
- `CardTemplate.spec`
- `TemplateBundle.files`
- skill / MCP registry
- run-local `library/skill_bindings.json` 和 `library/mcp.json`

这说明产品已经接近“可迁移方法包”的基础，只是还缺少一个明确的中间层，把“模板”和“实例”真正分开。

建议关系如下：

```text
skill / mcp library item
        ↓
portable card package
        ↓ import + resolve + bind
project card
        ↓ start run
run-local bindings
```

## Manager 与能力发现目标

这套 portable package 方案有一个配套前提：

- skill / MCP 不应常驻注册进 Manager 上下文
- Manager 只需要在必要时能查到它们

也就是说，Manager 不应“天生拥有所有 skill / MCP”，而应只拥有“发现与挂载能力”的工具。

这和 portable card package 的目标是一致的：

- package 保存方法依赖；
- Manager 在需要时解析依赖；
- wrapper 在运行时完成真正挂载；
- 不把整个能力库长期塞进对话上下文。

## 为什么不把 skill / MCP 常驻给 Manager

这样设计有四个直接原因：

1. 减少上下文占用
2. 避免 Manager 错把可选能力当成默认总是可用
3. 保持执行可复现，明确“这次 run 到底挂了哪些能力”
4. 让 portable package 在迁移后重新解析本地能力，而不是继承源项目的隐式上下文

如果把 skill / MCP 直接注册为 Manager 的常驻能力，会出现两个认知错位：

- Manager 会把“系统里可能存在的能力”误当成“这次执行已经启用的能力”
- 用户会分不清 package 依赖、project 配置、run-local 注入之间的边界

对于 portable package，这会进一步带来迁移歧义：

- 源机器的 skill / MCP 是否真的存在于目标机器
- 目标机器里是否只是同名不同版本
- 当前项目是否真的选择了这些能力

因此，正确边界应是：

- skill / MCP 作为系统能力库存在
- Manager 不常驻持有它们的全文或摘要
- Manager 在需要时通过搜索工具查找并选择

## 能力发现的推荐目标

如果用户的目标只是“不要把 skill / MCP 注册给 Manager 占上下文，只要 Manager 能搜到信息就行”，那么 discovery 层的核心目标应收敛为：

1. 不依赖常驻 prompt 注入
2. 不依赖 LLM 总结才能工作
3. 能用 skill 名、MCP 名、别名、runtime 线索做查询
4. 能在命中后读取对应自解释文件

换句话说，Manager 需要的是：

- 一个能力搜索入口
- 一个能力详情入口

而不是预装整座能力库。

## 自解释文件优先

skill / MCP 本身已经有自解释材料：

- skill 以 `SKILL.md` 为主
- MCP 以 `README.md`、`manifest.json`、`server.json` 等文件为主
- 少数 MCP 可能来自 runtime profile 派生，而不是独立源码目录

因此 discovery 的 source of truth 应优先是这些自解释文件，而不是二次生成摘要。

这意味着：

- 即使关闭 summarizer，能力发现仍应可用
- 即使 registry 摘要质量不好，Manager 仍应能靠名称和结构化信息定位能力
- 自解释文件应是详情读取的最终依据

## 推荐的 Discovery 设计

建议把能力发现拆成两层，而不是让 Manager 直接拿全文上下文。

### 1. Search Capability

职责：

- 搜索 skill / MCP 的自解释文件
- 返回轻量命中结果
- 不把全文直接塞进 Manager 上下文

最小结果建议包含（实现层规格见"现有 Discovery 工具现状诊断 → 推荐的 Search 返回字段"）：

- `id`
- `kind`
- `name`
- `summary_short`
- `match_reason`
- `supported_runtimes`
- `enabled`

这样 Manager 先知道：

- 系统里有没有这个能力
- 命中的是 skill 还是 MCP
- 为什么被命中
- 是否和当前 runtime 有关

### 2. Get Capability Detail

职责：

- 在需要时读取单个 skill / MCP 的自解释文件
- 返回结构化提取和少量关键片段
- 供 Manager 做最终判断

这样 Manager 只在真正需要时才读取说明书，不会把整个能力库长期带进上下文。

## 为什么不建议只做“全文 grep”

表面上看，“给 Manager 一个搜索工具去搜自解释文件”已经足够。但如果实现成纯全文搜索，后续会有三个问题：

1. MCP 的自解释来源不统一
2. skill / MCP 的稳定 id、别名、runtime 兼容性不一定能从全文命中里直接得出
3. Manager 需要的是“可挂载的能力条目”，不是一堆原始文本片段

因此更稳的实现目标不是纯 grep，而是：

- 底层来源仍然是自解释文件
- 上层提供一层轻量结构化搜索
- 详情再按需读取原始说明文件

这既满足“不注册给 Manager”，也避免“搜得到文本但用不起来”。

## Registry 与 Summarizer 的角色收缩

在这个设计下，registry 和 summarizer 都可以退到次要位置。

### Registry

registry 仍然可以存在，但职责应收缩为：

- 缓存扫描结果
- 缓存结构化字段
- 提供稳定 id 到 source 的映射

而不是：

- 作为 Manager 唯一可见的能力真相
- 作为 discovery 成败的唯一前提

### Summarizer

summarizer 应视为可选增强，而不是 discovery 基础设施。

它可以用于：

- 生成更易读的 `summary_short`
- 提升搜索召回
- 改善 UI 展示

但不应决定：

- skill / MCP 是否能被发现
- Manager 是否能定位并挂载能力

## 现有 Discovery 工具现状诊断

当前有 6 个工具，按 kind 对称分裂：

| 层级   | Skill                                        | MCP                       |
|--------|----------------------------------------------|---------------------------|
| List   | list_skill_library → id, kind, name, enabled | list_mcp_library → 同     |
| Search | search_skill_library → 同上 + score          | search_mcp_library → 同   |
| Detail | get_skill_library_item → 全量 dump           | get_mcp_library_item → 同 |

两个问题：

1. Search 返回太少 — id, kind, name, enabled 四个字段不够 Manager 判断"要不要读详情"。没有 match_reason，没有 supported_runtimes，Manager 只能盲猜或每次都 follow up get_*，浪费一轮 tool call。
2. Detail 返回太杂 — model_dump() 把 source_hash、generated_by、generated_at、metadata 这些内部管理字段全部暴露给 Manager，增加上下文噪音但不帮助挂载决策。

### 推荐的 Search 返回字段

每次搜索命中返回以下字段，单条约 150-200 tokens：

```json
{
  "id": "single-cell-plotting",
  "kind": "skill",
  "name": "Single-cell Plotting",
  "summary_short": "提供单细胞可视化相关的绘图能力",
  "match_reason": "name: single-cell, tags: visualization",
  "supported_runtimes": ["omicverse", "scanpy"],
  "enabled": true
}
```

字段选择逻辑：

| 字段               | 为什么留                                                                                                      | 为什么不留                                                                       |
|--------------------|---------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| id                 | Manager 挂载时的唯一标识                                                                                      | —                                                                                |
| kind               | 区分 skill / MCP，挂载路径不同                                                                                | —                                                                                |
| name               | 人类可读名                                                                                                    | —                                                                                |
| summary_short      | 一行描述，帮 Manager 判断相关性                                                                               | —                                                                                |
| match_reason       | 新增。解释为什么命中，省掉 Manager 猜测                                                                       | —                                                                                |
| supported_runtimes | 新增到 search 层。Manager 做挂载决策的关键字段 — 如果 card 用 omicverse 而 skill 只支持 scanpy，search 层就能排除 | —                                                                                |
| enabled            | 标记是否可用                                                                                                  | —                                                                                |
| score              | —                                                                                                             | Manager 不需要知道内部打分算法的数值，match_reason 比 0.8732 更有用              |
| tags               | —                                                                                                             | match_reason 已经包含命中的 tags；未命中的 tags 是噪音                           |
| use_cases          | —                                                                                                             | 留给 detail                                                                      |
| source_path        | —                                                                                                             | 内部路径，Manager 不需要                                                         |
| summary_long       | —                                                                                                             | 留给 detail                                                                      |

### match_reason 的实现

不需要复杂 NLP，用当前 `_score_entry` 已有的匹配信息拼装即可：

```python
def _build_match_reason(entry, query_terms, runtime_filter, tag_filters):
    """query_terms, runtime_filter, tag_filters 均已经过 _normalize_text()。
    entry 侧字段也统一用 _normalize_text() 处理，确保空格压缩、标点去除等行为一致。"""
    parts = []
    norm = _normalize_text  # 统一用同一个 normalizer 处理两边
    # 哪些 query terms 命中了 name
    name_hits = [t for t in query_terms if t in norm(entry.name)]
    if name_hits:
        parts.append(f"name: {', '.join(name_hits)}")
    # 哪些 query terms 命中了 summary
    summary_hits = [t for t in query_terms if t in norm(entry.summary_short or "")]
    if summary_hits:
        parts.append(f"summary: {', '.join(summary_hits)}")
    # tag 交集 — 两边都走 _normalize_text 避免空格/标点不匹配
    tag_hits = tag_filters & {norm(t) for t in entry.tags}
    if tag_hits:
        parts.append(f"tags: {', '.join(tag_hits)}")
    # runtime 命中
    if runtime_filter and runtime_filter in [norm(r) for r in entry.supported_runtimes]:
        parts.append(f"runtime: {runtime_filter}")
    return "; ".join(parts) if parts else "broad match"
```

这和 `_score_entry` 的循环几乎重叠，可以在同一次遍历中完成，不增加额外复杂度。

### 推荐的 Detail 返回字段

单条约 400-600 tokens：

```json
{
  "kind": "skill",
  "item": {
    "id": "single-cell-plotting",
    "name": "Single-cell Plotting",
    "summary_short": "提供单细胞可视化相关的绘图能力",
    "summary_long": "该 skill 提供 UMAP/tSNE/violin/... 等标准单细胞可视化...",
    "use_cases": ["UMAP 降维可视化", "细胞类型注释展示"],
    "compatibility_notes": ["需要 matplotlib >= 3.7"],
    "supported_runtimes": ["omicverse", "scanpy"],
    "runtime_requirements": ["python >= 3.10"],
    "launch_hint": "通常通过 configure_card_execution 的 skills 字段挂载",
    "enabled": true
  }
}
```

与当前全量 dump 的对比：

| 字段                 | Detail 保留？ | 理由                                                              |
|----------------------|---------------|-------------------------------------------------------------------|
| id, name, kind       | 保留          | 基础标识                                                          |
| summary_short        | 保留          | 快速回顾                                                          |
| summary_long         | 保留          | Detail 层才需要的完整摘要                                         |
| use_cases            | 保留          | Manager 判断"这个能力适不适合当前 card"                           |
| compatibility_notes  | 保留          | 挂载风险评估的关键字段，当前被 dump 出去但 Manager 没有结构化关注 |
| supported_runtimes   | 保留          | 运行时兼容性                                                      |
| runtime_requirements | 保留          | 运行时前提条件                                                    |
| launch_hint          | 保留          | MCP 特有 — 告诉 Manager 怎么启动                                  |
| enabled              | 保留          | 可用性                                                            |
| source_path          | 剔除          | 内部路径，Manager 不需要知道文件在哪                              |
| source_hash          | 剔除          | 内部管理字段                                                      |
| generated_by         | 剔除          | 谁生成的摘要不影响挂载                                            |
| generated_at         | 剔除          | 同上                                                              |
| metadata             | 剔除          | 内部元数据（root、source 等）                                     |

实现上不需要新模型，只需一个 `_serialize_detail_entry` 静态方法做白名单选取。

### 是否合并 Skill / MCP 工具

当前 6 个工具（skill × 3 + MCP × 3）。前文建议的 "Search Capability" 和 "Get Capability Detail" 是统一的。

建议 v1 保持分拆，不合并。理由：

1. Manager 系统提示已经有明确的 skill vs MCP 概念区分，合并反而增加歧义
2. `configure_card_execution` 的入参就是分开的 skills 和 mcp_servers 字段，搜索和挂载保持一致更自然
3. 合并后 kind 参数变成必填，增加 Manager 调用复杂度
4. 如果 Manager 不确定应该搜 skill 还是 MCP，说明它还没理解 card 需求 — 这时候 list 两边各一次也就两次 tool call，成本可控

但可以加一个优化：在 search 层允许跨 kind 搜索，新增一个 `search_capability` 工具内部同时查 skill 和 MCP，返回混合结果。这是 v2 可选优化，不阻塞 v1。

### 上下文成本估算

按 top_k=5 的典型搜索场景：

| 层级           | 单条 tokens | 5 条总计         | 当前值                     |
|----------------|-------------|------------------|----------------------------|
| Search（推荐） | ~180        | ~900             | ~200（太精简，缺决策信息） |
| Detail（推荐） | ~500        | N/A（单次 1 条） | ~800（太多内部字段）       |
| List           | ~30         | ~150             | ~150（已合理）             |

对比当前：search 返回 4 字段 → Manager 经常需要 follow up detail → 实际成本是 200 + 800 = 1000 tokens 加一轮延迟。推荐方案 search 返回 900 tokens 但包含足够决策信息，可以减少不必要的 detail 调用。

### 建议的改动范围

最小改动集，不需要动模型层：

1. `_serialize_minimal_entry` → 新增 `_serialize_search_entry`，返回上面推荐的 7 个字段
2. `search_entries` → 用 `_serialize_search_entry` 替换 `_serialize_minimal_entry`，加上 match_reason 生成
3. `_serialize_entry` → 新增 `_serialize_detail_entry`，做白名单字段选取
4. `get_entry` → 用 `_serialize_detail_entry` 替换全量 dump
5. `list_entries` → 保持 `_serialize_minimal_entry` 不变，list 仍然最轻量

改动集中在 `library_registry_service.py` 的 4 个序列化方法，不影响 `LibraryEntry` 模型本身。

## 对 Portable Package 的影响

portable card package 若引用 skill / MCP，实例化时的能力解析也应遵循同样原则：

- 先通过 discovery 工具按 id / alias / runtime 解析本地能力
- 命中后按需读取详情
- 最后再写入 card 的 `executor_context`

不要让 package 实例化依赖一份必须预先生成好摘要的 registry。

否则迁移时会出现一个不必要的脆弱点：

- 目标机器的能力其实在
- 但 registry 没刷新或摘要质量差
- 结果 package 被误判成“找不到依赖”

## 推荐的 v1 边界（Discovery 层）

> 完整的 v1 范围总表见"推荐的 v1 与后续边界"章节。本节仅列出 discovery 层的最小目标。

如果要做最小版本，建议先做到：

1. Manager 不常驻注入 skill / MCP
2. 提供按需搜索自解释文件的能力搜索工具
3. 提供单条能力详情读取工具
4. 支持按 `id / name / alias / runtime` 查询
5. summarizer 可关闭，不影响 discovery 基本可用

先不要做：

- 把所有能力全文常驻注入 Manager
- discovery 强依赖 LLM 生成摘要
- 把 registry 做成复杂数据库索引系统
- 让 package 实例化依赖人工先跑一次“注册 + 总结”

## 对象模型

### 1. Library Item

系统能力注册条目，继续沿用现有 skill / MCP library 设计。

建议新增 `aliases: list[str]` 字段，用于承载非正式别名（如缩写、旧名、领域术语），与 `name`（唯一正式名）和 `tags`（分类标签）区分。aliases 由 skill / MCP 作者在自解释文件中声明，registry 扫描时提取。

职责：

- 提供可搜索、可挂载能力；
- 提供摘要和兼容性信息；
- 在运行时被 wrapper 解析为实际注入。

不负责：

- 保存项目资产绑定；
- 保存某个 card 的业务目标；
- 充当可迁移 card 本体。

### 2. Portable Card Package

新增概念，表示一张可跨项目、跨用户迁移的方法牌。

职责：

- 描述做什么；
- 描述需要什么输入；
- 描述会产出什么输出；
- 描述推荐 skill / MCP / runtime；
- 携带少量配套文件；
- 提供导入时的验证与绑定约束。

不负责：

- 保存项目内运行状态；
- 保存具体 run 历史；
- 直接假设目标环境一定可用。

### 3. Project Card

现有 card 继续作为项目实例。

职责：

- 绑定本项目资产；
- 保存状态、step、runs、manager review；
- 接收配置后的 executor_context；
- 进入 run / review / accepted 生命周期。

## 推荐的数据边界

### package 内应该保存的内容

- 包元数据
- 目标与摘要
- 输入 schema
- 输出 schema
- 参数 schema
- 推荐 `skills`
- 推荐 `mcp_servers`
- runtime requirements
- `instruction_blocks` / `script_preference`
- 自定义 bundle files
- 导入时校验规则
- 版本与 provenance

### package 内不应该保存的内容

- 项目内 `asset_id`
- `linked_runs`
- `linked_assets`
- card status
- run result 路径
- secrets / API keys / cookies / tokens
- 用户 home 路径
- 机器特定 `source_path`
- 未脱敏环境变量

## Runtime 继承与冻结

portable card package 进入产品后，runtime 语义必须比当前更清楚，否则 package、project default 和 card override 会互相打架。

建议明确区分三层 runtime：

1. app-level recent preference
2. project runtime preference
3. card runtime override

### 1. app-level recent preference

这是“最近一次用户明确设置过的 runtime 偏好”。

职责：

- 作为新项目创建时的初始化来源；
- 提供“半全局”体验，让新项目默认跟随用户最近一次使用习惯；
- 不直接参与已存在项目的运行时决策。

它不是项目事实，也不是 card 事实。

### 2. project runtime preference

这是项目级默认 runtime。

职责：

- 作为当前项目中新 card / 未覆盖 card 的默认运行时来源；
- 反映当前项目通常使用哪套 Python / R runtime；
- 作为 Manager 规划时的首选上下文。

它应在项目内持久化，但不应被后续 app-level recent preference 回写覆盖。

### 3. card runtime override

这是 card 级显式绑定。

职责：

- 覆盖项目默认 runtime；
- 表示这张 card 因方法依赖、兼容性或复现性，需要固定使用某个 runtime。

一旦 card 已显式设置 runtime，它就不应随着 project runtime 或 recent preference 改动而漂移。

## 推荐的 Runtime 继承规则

建议采用以下规则：

1. 新建项目时：
   - 用 app-level recent preference 初始化 project runtime preference
2. 新建 card 时：
   - 默认继承 project runtime preference
3. card 未显式设置 runtime 时：
   - 执行使用 project runtime preference
4. card 已显式设置 runtime 时：
   - 执行使用 card runtime override
5. 用户之后修改 project runtime preference 时：
   - 只影响尚未显式绑定 runtime 的 card
   - 不回写已显式绑定的 card

这正好对应“新的项目默认跟随上一次项目设置，但已经设置好的 runtime 不会变”。

## 为什么这对 Portable Package 很重要

portable card package 会把 runtime requirement 带进新项目。如果系统没有清楚的继承层次，就会出现三种歧义：

1. package 推荐 runtime 和项目默认 runtime 谁优先
2. card 一旦实例化后，后续项目 runtime 改动是否应影响它
3. 用户看到“系统默认”时，到底是 system runtime，还是“跟随项目默认”

因此 package 设计中应避免把 `__system__` 和“follow project default”混成一个概念。

建议在语义上拆开：

- `__system__`
  - 表示显式使用系统运行时
- `follow_project_default`
  - 表示不做 card 级显式绑定，运行时由项目默认决定

第一阶段即使内部实现仍复用现有字段，文档语义也应先区分这两个概念，避免后续 UI、错误提示和迁移行为混乱。

## 推荐的 Package Manifest

建议新增一个独立 manifest，而不是直接复用当前 `Card` 或 `CardTemplate` JSON。

示意结构：

```json
{
  "schema_version": "portable_card_package.v1",
  "package_id": "singlecell-umap-basic",
  "version": "1.0.0",
  "title": "Single-cell UMAP",
  "summary": "对单细胞对象执行标准 UMAP 降维并输出图和摘要。",
  "description": "适用于已有 embedding 前处理结果的单细胞可视化 card。",
  "tags": ["single-cell", "umap", "visualization"],
  "compatibility": {
    "supported_runtimes": ["omicverse", "scanpy"],
    "required_skills": [],
    "optional_skills": ["single-cell-plotting"],
    "required_mcps": [],
    "optional_mcps": ["omicverse"]
  },
  "inputs_schema": [
    {
      "slot": "expression_object",
      "label": "Single-cell object",
      "accepted_formats": ["h5ad", "rds"],
      "required": true
    }
  ],
  "outputs_schema": [
    {
      "role": "run_preview",
      "artifact_class": "figure",
      "accepted_formats": ["svg", "png"],
      "required": true
    },
    {
      "role": "run_summary",
      "artifact_class": "document",
      "accepted_formats": ["md"],
      "required": true
    }
  ],
  "parameters": [
    {
      "name": "color_by",
      "type": "string",
      "required": false,
      "default": "cell_type"
    }
  ],
  "executor": {
    "skills": ["single-cell-plotting"],
    "mcp_servers": ["omicverse"],
    "script_preference": "Prefer reusable package scripts when compatible.",
    "runtime_requirements": {
      "python_runtime": "omicverse",
      "r_runtime": "__system__"
    },
    "instruction_blocks": [
      "Write generated scripts under the run-local generated script directory.",
      "Do not modify project graph files."
    ]
  },
  "bundle": {
    "files": [
      {
        "path": "scripts/umap_template.py",
        "description": "Reusable UMAP starter script"
      }
    ]
  },
  "provenance": {
    "source_template_id": "tpl_singlecell_umap_v1",
    "created_at": "2026-06-11T00:00:00Z",
    "created_by": "manager",
    "content_hash": "sha256:..."
  }
}
```

**字段解析规则：**

- `compatibility.supported_runtimes`：搜索与展示用途。表示"这些 runtime 中的任意一个都能运行本 package"，不区分 Python / R，用于 search 层快速过滤和 UI 标签展示。
- `executor.runtime_requirements`：resolve 与执行用途。按 Python / R 分别声明执行时的具体 runtime 绑定需求，resolve 阶段以此为准。
- 当两者出现表面矛盾时（如 `compatibility` 包含 omicverse 但 `executor` 指定 scanpy），以 `executor.runtime_requirements` 为权威来源；`compatibility` 只是搜索召回的宽松提示。

## skill / MCP 在 package 中的表达方式

### 默认策略：引用，不复制

package 对 skill / MCP 的默认表达应是：

- 记录 registry id
- 记录 required / optional
- 记录兼容性说明

而不是：

- 打包 `SKILL.md` 原文
- 打包 MCP 服务源码
- 打包目标机器不可复用的本地路径

原因很直接：

- skill / MCP 本身属于系统能力库；
- 它们有独立生命周期、版本和权限边界；
- 把它们整体复制进 card package 会造成版本漂移和维护断裂。

### 例外策略：只允许 package-local adapter 文件

有些场景下 package 需要自带一层很薄的 glue code，例如：

- MCP 调用前的数据映射模板
- skill 使用说明片段
- 参数转换脚本

这些可以随 package 一起走，但必须满足：

- 文件完全位于 package 内；
- 不依赖目标机器私有绝对路径；
- 不包含认证信息；
- 不替代系统 registry 的主能力定义。

## 自定义文件的边界

用户提到“可以适当携带一些自定义文件”，这是可行的，但必须严格收敛。

建议只允许三类文件：

1. 模板脚本
2. prompt / instruction 片段
3. 小型静态资源或配置模板

不建议允许：

- 大体积数据文件
- 二进制依赖环境
- 私有证书 / 密钥
- 与用户目录强绑定的 shell 启动器
- 需要 root 或系统安装步骤的外部组件

初期建议加硬限制：

- 文件数限制
- 总大小限制
- 只允许文本类与白名单格式

这样更容易控制 package 的迁移稳定性和审计成本。

## Package 存储路径约定

导入后的 package 应存储在项目 `_system` 目录下，与现有 registry / template 平行：

```text
_system/packages/index.json                          # 轻量索引（id, version, title, tags）
_system/packages/{package_id}/{version}/manifest.json  # 完整 manifest
_system/packages/{package_id}/{version}/bundle/        # bundle 文件目录
```

设计要点：

- 两级存储：search / list 只读 `index.json`，detail / resolve 才读完整 manifest。
- 每个版本独立目录，支持多版本共存。
- bundle 目录内使用 package-local 相对路径，不引用项目外资源。
- `index.json` 在 import / delete 时更新，保持与实际 manifest 一致。

## 导入与实例化流程

推荐把“发牌”拆成两个阶段。

### 阶段 1：导入 package

把 package 放入目标系统后，先只完成元数据注册，不直接创建运行中的 card。

导入时检查：

- manifest schema 是否有效
- package content hash 是否正确
- bundle 文件是否完整
- skill / MCP 引用 id 是否能在本地 registry 中找到
- runtime requirement 是否有潜在候选

导入结果应是：

- `ready`
- `ready_with_warnings`
- `blocked`

### 阶段 2：实例化为 project card

用户或 Manager 从 package 创建项目 card 时，再做项目级绑定：

- 选择输入资产
- 解析参数
- 选择可用 runtime
- 确认 optional skill / MCP 是否挂载
- 生成本项目 card 的 `executor_context`

实例化后的 card 才进入现有项目生命周期。

### 实例化时的 runtime 绑定策略

实例化 package 时，不应默认把 package 的 runtime requirement 直接固化成 card override。

更稳的策略是分三类：

1. package 只是“推荐 runtime”
   - 若当前 project runtime preference 已满足，则 card 继续 follow project default
   - 不产生 card override
2. package 有“强依赖 runtime”
   - 若项目默认不满足，则实例化时写入 card runtime override
3. package 要求系统 runtime
   - 应显式标记为 `__system__`
   - 不应和“follow project default”混淆

这样可以避免 package 导入后把每张 card 都变成硬编码 runtime，导致项目默认失去作用。

## Resolve / Bind 机制

这里是整个方案里最关键的一层。

package 可迁移，不代表 package 能直接执行。导入后必须 resolve：

1. 能力解析
2. 运行时解析
3. 资产绑定
4. 路径重写

### 1. 能力解析

把 package 里的 `skills` / `mcp_servers` 从逻辑引用解析为本地 registry 条目。

可能结果：

- 全部满足
- 部分满足，optional 缺失
- required 缺失

### 2. 运行时解析

把 package 的 runtime requirement 映射到目标环境中的可用 runtime profile。

注意：

- `omicverse` 在源机器和目标机器上可能只是同名，不一定同内容；
- `__system__` 只能表示“不要求显式绑定”，不能表示“保证可运行”；
- 如果 package 依赖特定 R 包或 Python 包，不能只靠名字猜测成功。

### Runtime Dependency Resolver 集成

这里不应只做 runtime 名称匹配，而应复用现有 runtime dependency resolver 的确定性规划能力。

原因：

- package 依赖的不是一个“名字”，而是一组真实可用的包、解释器和 solver 能力；
- `omicverse` 这个 runtime 名称存在，不代表里面真的已经有 `scanpy`、`harmonypy` 或特定 R 包；
- 如果 resolve 阶段只看 runtime 名字，实例化结果会过于乐观，错误会被推迟到 run 阶段才暴露。

因此 package 实例化时的 runtime 解析应优先复用现有 resolver 能力，至少回答：

- 目标 runtime 是否存在；
- package 声明的 Python / R 依赖是否已经满足；
- 若不满足，哪些依赖可自动安装，哪些需要人工准备；
- 当前 fallback policy 是否允许进入自动安装链路。

推荐把 package runtime 解析与现有 resolver 对齐为两层：

1. `runtime presence`
   - runtime 名称和路径是否可解析
2. `dependency satisfaction plan`
   - 对 package 依赖生成类似 resolver plan 的结构化判断

这样 package 的 resolve 结果可以与现有 `install_runtime_dependencies` / `resolve_runtime_dependencies` 工作流自然接轨，而不是另起一套名字匹配逻辑。

### Runtime Dependency State 联动

package 实例化或预检查时，还应联动现有 runtime dependency state。

原因：

- 同一 runtime 的依赖修复可能已经在后台安装中；
- 同一 package set 可能刚失败过，并已有去重 key、失败详情和 retry hint；
- 如果 package resolve 完全忽略这些状态，用户会看到与系统真实状态脱节的结果。

因此 resolve 结果应允许出现中间态，而不是只有“可用 / 不可用”两种结果。

推荐至少区分：

- `ready`
- `ready_with_warnings`
- `waiting_for_runtime_dependency_job`
- `blocked_by_runtime_dependency_failure`
- `blocked_by_runtime_missing`

其中：

- `waiting_for_runtime_dependency_job`
  - 表示 package 需要的依赖正在安装，不应重复触发同一安装
- `blocked_by_runtime_dependency_failure`
  - 表示已有同 runtime / package set 的终态失败记录，应把失败详情和 retry hint 暴露给用户

这使 package resolve 能和现有 runtime dependency state 形成闭环，而不是把正在进行或已失败的安装工作当成“系统还没看见”。

这里还应补一条：

- resolve 完成后，系统必须能给出本次 card 的 `effective runtime`，而不是只告诉用户“缺依赖”。

建议在 resolve / bind 结果里显式保存：

- `effective_python_runtime`
- `effective_r_runtime`
- `runtime_source`
  - `app_recent_default`
  - `project_default`
  - `card_override`
  - `package_requirement`

这样无论是导入诊断、card 详情还是 run 失败回显，都能解释“这次到底用了哪套 runtime，以及它从哪里来的”。

### `follow_project_default` 的内部表示

仅在文档语义上区分 `__system__` 和 `follow_project_default` 还不够，内部数据模型也必须能承载这种差异。

当前 runtime binding 只有：

- `conda_env: str | None`
- `r_env: str | None`

这会让 `None` 同时承担两种语义：

- 显式 system runtime
- 未覆盖，继续跟随 project default

这会直接破坏三层继承规则。

因此 v1 至少需要一种明确表示：

1. 新增 `runtime_source` 字段
2. 或在 runtime binding 中引入 sentinel 值
3. 或在 resolve 结果中保存未折叠前的来源信息，再在运行前展开

目标不是立刻敲定最终字段形式，而是先明确：

- `__system__` 和 `follow_project_default` 绝不能继续共用一个 `None` 语义
- card 是否显式覆盖，必须是可持久化、可审计、可解释的状态

### Effective Runtime 的持久化

文档前面要求把 `effective_python_runtime`、`effective_r_runtime`、`runtime_source` 暴露出来，这一步不能只停留在内存推导。

至少应持久化两类记录：

1. card / instantiate 级解析结果
2. run-local 级解析结果

推荐新增一个 run-local 文件，例如：

```text
runs/<run_id>/runtime_resolution.json
```

建议记录：

- requested package runtime requirement
- project runtime preference snapshot
- card runtime override snapshot
- effective Python runtime
- effective R runtime
- runtime source
- dependency resolver plan summary
- linked runtime dependency job / failure state when present

这样后续无论是用户看报错、reviewer 看执行上下文，还是 Manager 做 repair，都能基于持久化事实，而不是重新猜测一次解析过程。

## Runtime 可解释性与错误报文

runtime 一旦有三层继承关系，错误报文就不能只说“system 缺依赖”或“runtime 依赖不足”。

至少需要回答三件事：

1. 本次 run 的 effective runtime 是什么
2. 这个 runtime 来自哪里
3. 缺的是 runtime 内包，还是系统级工具

### 推荐的错误上下文

当出现依赖缺失时，建议统一携带：

- effective Python runtime
- effective R runtime
- runtime source
- missing dependency kind
  - python package
  - R package
  - system executable
  - external service / auth

示意：

```text
Runtime dependency missing.
Effective Python runtime: omicverse
Effective R runtime: follow_project_default -> R_env
Runtime source: card_override (Python), project_default (R)
Missing dependency kind: python package
Missing packages: scanpy, harmonypy
```

### 为什么必须这样做

如果系统只返回“依赖不足”或“system 依赖缺失”，用户会分不清：

- 这次 run 是真的用 system 在跑；
- 还是用了他手动设置的 conda / forge runtime；
- 还是 card override 悄悄覆盖了 project default。

这不仅影响用户修错，也会直接影响 package 的迁移体验。用户迁入一张 card package 后，如果运行失败却看不出实际 runtime 来源，就很难判断问题在 package、project default，还是 card override。

### 3. 资产绑定

package 只声明输入槽位，实例化时才绑定成项目资产。

例如：

- `expression_object` 绑定为本项目 `asset_id=adata_v3`
- `marker_table` 绑定为本项目 `asset_id=markers_v1`

### 4. 路径重写

package bundle 内若引用内部文件路径，导入后应转换为 package-local 存储路径，而不是沿用源机器路径。

## 失败与降级策略

这个系统必须允许“迁移成功但未完全可执行”的中间态。

建议显式区分：

### 1. Import Warning

例如：

- optional MCP 缺失
- 推荐 skill 不存在
- 有更高版本 runtime，但低版本仍可尝试

这种情况允许继续导入和实例化。

### 2. Import Blocker

例如：

- required skill 不存在
- manifest 损坏
- bundle 文件缺失
- package 使用未支持 schema version

这种情况不允许实例化。

### 3. Runtime Risk

例如：

- runtime 名称存在，但依赖完整性未知
- 需要联网能力但当前执行器受限
- MCP 条目存在，但认证状态未知

这种情况允许实例化，但要在 UI 和 Manager 认知里暴露为风险，而不是假装没问题。

### Runtime 相关中间态也属于降级策略

除了普通 import warning / blocker，package 还应能表达 runtime 相关的中间态：

- runtime 依赖正在后台修复；
- runtime 依赖刚失败，且存在非重试型 blocker；
- runtime 本体存在，但 resolver 只能证明“部分可安装”；
- MCP 所需 runtime 存在，但 MCP 配置尚未能生成。

这些状态不应被压缩成单一的 “runtime unavailable”。

## 与现有 CardTemplate 的关系

现有 `CardTemplate` 很接近这个目标，但语义上还不够清晰。

当前问题：

- 它既像“模板”，又部分像“复制 bundle 的实例来源”；
- 它还没有明确的“跨用户可迁移 package”边界；
- 它没有显式区分 required / optional 的 skill / MCP 兼容性；
- 它对导入阶段的 resolve / bind 模型还不完整。

建议方向不是推翻 `CardTemplate`，而是：

1. 保留 `CardTemplate` 作为 Manager 侧模板抽象
2. 新增 `PortableCardPackage` 作为迁移与分发抽象
3. 明确：
   - `CardTemplate` 偏系统内复用与市场
   - `PortableCardPackage` 偏跨项目 / 跨用户迁移

如果后续想收敛模型，也可以让 `PortableCardPackage` 成为 `CardTemplate` 的一种可导出版，而不是一开始就强行二者合一。

## Package 导出路径

前文主要讨论了导入和实例化，但 portable package 必须也有明确的导出来源。

当前最接近导出入口的是：

- 从现有 card 构建 `CardTemplate`
- 持久化 `TemplateBundle`

因此建议把 v1 导出路径定义为：

```text
accepted / reusable card
  -> CardTemplate
  -> PortableCardPackage export
```

### 导出时应保留的内容

- 通用的 card 标题 / summary / why 模板
- 输入输出 schema
- executor context 中与 skill / MCP / runtime requirement 相关的可迁移部分
- bundle files
- script asset requirement schema
- 方法级 instruction blocks

### 导出时应剥离或脱敏的内容

- `source_card_id`
- `source_project_id`
- 项目内 asset_id
- run 历史
- 本地绝对路径
- 与当前用户绑定的 references
- secrets 与任何未脱敏环境变量

### Bundle 文件重定位

template bundle 当前是以存储路径保存的，而 portable package 需要 package-local 的相对布局。

因此导出规则应明确：

- 从 template bundle `stored_path` 映射到 package 内部相对路径
- 去掉 project-specific 的目录结构假设
- 保留 path rewrite 关系，但 rewrite 的目标必须是 package-local 路径

一句话说，template 是系统内复用格式，package 是迁移分发格式，二者不能假定路径布局完全相同。

## MCP 配置生成的泛化要求

package 若引用 MCP，解析逻辑不能只支持 `omicverse` 这一种特例。

当前底层如果只对单个 MCP 做硬编码配置生成，会导致：

- package 解析能找到 MCP id
- 但 run-local 无法生成真正可用的 MCP config
- 最终实例化看似成功，运行时却失去 MCP 能力

因此 v1 前建议明确：

- MCP 配置生成必须从通用自解释来源读取
- 优先支持 `server.json` / `manifest.json` / 等价结构化文件
- `omicverse` 这类 runtime-derived MCP 可以保留特例，但不能成为唯一实现

推荐顺序：

1. 先支持通用 manifest 驱动的 MCP config 生成
2. 再保留少数 runtime profile 特例作为补充

这样 package 的 MCP 解析才真正符合“按 id/alias 解析本地能力”的目标。

## Manager 的 Package 工具骨架

如果要让 Manager 能区分“搜索能力库”和“搜索可迁移 package”，文档里至少要给出 package 相关工具的最小骨架。

建议 v1 预留以下工具：

- `search_card_packages`
- `get_card_package_detail`
- `import_card_package`
- `instantiate_card_package`

### `search_card_packages`

输入建议：

- `query`
- `tags?`
- `runtime?`
- `top_k?`

输出建议：

- `package_id`
- `version`
- `title`
- `summary`
- `compatibility`
- `match_reason`

### `get_card_package_detail`

输入建议：

- `package_id`
- `version?`

输出建议：

- 完整 manifest
- bundle 文件列表
- runtime requirement
- required / optional skill / MCP
- importability / resolve summary

### `import_card_package`

输入建议：

- package source
  - 本地文件
  - 本地目录
  - 未来可扩展为 registry / marketplace entry
- `overwrite?`

输出建议：

- import status
- package id / version
- warnings
- blockers

### `instantiate_card_package`

输入建议：

- `package_id`
- `project_id`
- 输入资产绑定
- 参数绑定
- runtime preference override?

输出建议：

- 新建 card id
- resolve result
- warnings / blockers
- 产生的 card runtime state 摘要

## 安全边界

这个功能一旦做出来，很容易把“模板分发”变成“隐式执行载体”。所以安全边界要先写清楚。

必须禁止 package 携带：

- API keys
- access tokens
- cookies
- 明文数据库密码
- SSH key
- host-specific absolute path contract
- 任意可执行二进制安装包

必须审计 package 携带的：

- shell 脚本
- Python / R 模板脚本
- prompt 注入片段
- 外部 URL 引用

推荐把 package 设计成：

- 内容可列举
- hash 可验证
- 导入可审计
- 运行依赖显式暴露

## Bundle 文件的安全扫描

package 若允许携带 bundle 文件，就不能只靠“人工目测”。

v1 至少应定义一套轻量扫描规则，覆盖三类风险：

1. 可执行脚本风险
2. 外部 URL / 网络引用风险
3. prompt / instruction 注入风险

### 脚本扫描的最低要求

对于 shell / Python / R / JS 等文本脚本，至少应扫描明显高风险模式，例如：

- `eval(`
- `exec(`
- `os.system(`
- `subprocess`
- `system(`
- `shell=True`
- 动态下载并执行

这里的目标不是做完备静态分析，而是尽早标记需要人工确认的 bundle。

### URL 引用策略

外部 URL 不应默认阻断，但应分级：

- 无 URL：通过
- 普通文档 URL：warning
- 可执行脚本下载、远程安装、未知二进制地址：block 或强 warning

如果后续有白名单策略，建议优先支持：

- 官方文档站
- 已知 registry / package index
- 团队自有可信域名

### Prompt / Instruction 片段检查

prompt 片段至少应检查明显的注入型内容，例如：

- 覆盖系统规则
- 要求忽略安全限制
- 要求外传 secrets
- 强制联网下载或修改受保护目录

这类检测可以先做关键词级启发式，不需要一开始就做复杂分类器。

## 版本与兼容性

这是另一个必须提前考虑的点。

建议 package 至少具备三种版本语义：

1. schema version
2. package version
3. dependency expectation

例如：

- `schema_version=portable_card_package.v1`
- `version=1.2.0`
- `compatibility.supported_runtimes=["omicverse>=1,<3"]`

即使第一阶段不做完整 semver 解析，也应先保留字段，不要把兼容性信息埋进自然语言摘要里。

## 性能路线

文档前面的能力发现和 package 解析设计，在条目数量增长后会遇到明显性能问题。这里建议先把高投入产出比的优化点列出来，避免后续实现完全按线性扫描扩张。

### 1. 搜索倒排索引

当前 discovery / registry 搜索如果继续走全量线性扫描，skill、MCP、package 数量一上来就会退化。

建议：

- 为 `id`、`name`、`aliases`、`tags`、`supported_runtimes` 建立轻量倒排索引
- 查询时先缩小候选集，再做精细打分

这不需要引入外部搜索引擎，纯 Python 就够。

### 2. 增量 refresh

registry refresh 不应永远是全量重建。

建议：

- 保存 `mtime + source_hash` 索引
- 先比较 `mtime`
- 只有变化项才重新算 hash
- 只有 hash 变化的条目才重新做摘要增强

这样可以显著减少 I/O 和 summarizer 调用。

### 3. Resolve 并行化

package resolve 里的几步并不全是串行依赖。

特别是：

- 能力解析
- runtime dependency resolver 规划

这两步原则上可以并行。

因此建议在实现时保留并行化空间，而不是把所有检查强行串成单线程长链路。

### 4. Summarizer 批量或并发

如果保留 summarizer 增强能力，refresh 多个条目时不应强制单条串行调用。

建议：

- 支持 batch summarization
- 或至少支持并发 summarization

这样它才不会成为 registry refresh 的主要瓶颈。

### 5. Package 两级存储

package 若也像 registry 一样全部挤在一个大 JSON 中，list/search 会很快变重。

建议从一开始就按两级存储设计：

- 轻量索引文件
- 每个 package 的独立 manifest

搜索只读索引，详情才读完整 manifest。

### 6. Bundle 复用拷贝优化

package / template 实例化若总是物理复制 bundle 文件，会带来不必要的 I/O 和磁盘占用。

因此后续实现可优先考虑：

- 同文件系统硬链接
- 支持时使用 reflink / CoW
- 复制作为保底 fallback

这属于实现优化，不影响 v1 语义，但应提前在设计里留出空间。

## 推荐的 v1 与后续边界

综合功能缺口和性能投入，建议优先级如下：

### v1 必须覆盖

**Package 核心：**

- 定义 package manifest
- 支持 package 导入与本地存储
- 支持引用 skill / MCP id
- 支持小型 bundle files
- 支持导入校验
- 支持实例化成普通 card
- package 导出路径定义

**Runtime 与 Resolve：**

- runtime dependency resolver 集成到 package resolve
- runtime dependency state 中间态暴露
- `follow_project_default` 的内部表示
- effective runtime 的持久化记录

**Discovery 与工具：**

- Manager package 工具骨架
- 通用 MCP 配置生成至少覆盖 manifest 驱动路径
- search 返回字段扩充（summary_short, match_reason, supported_runtimes）
- detail 返回字段白名单化（剔除内部管理字段）

### v1 建议覆盖

- bundle 文件的轻量安全扫描
- capability / package 搜索的轻量索引
- 增量 refresh

### v1 先不做

- package 内嵌完整 skill 本体
- package 内嵌完整 MCP server
- 自动下载依赖
- 跨系统自动修复 runtime
- package 间依赖解析

### v2 再优化

- summarizer batch / 并发
- resolve 深度并行化
- bundle 硬链接 / reflink 优化
- 更强的静态安全分析
- 跨 kind 统一搜索工具（search_capability）

## UI / Manager 行为建议

虽然本轮不落 UI，但行为边界应先确定。

Manager 应该能区分三件事：

1. 搜索能力库
2. 搜索可迁移 card packages
3. 把 package 实例化为当前项目 card

不要把 skill / MCP 搜索和 package 搜索混成一个概念。

package 详情页应优先展示：

- 它做什么
- 需要哪些输入
- 会产出什么
- 依赖哪些 skill / MCP / runtime
- 哪些依赖是 required，哪些是 optional
- 是否携带 bundle files

## Open Questions

1. runtime requirement 初期只支持精确 id，还是允许简单版本表达式？
2. required MCP 缺失时，是否允许”实例化但禁止 start run”？
3. package 的来源是否需要签名机制，还是先只做 hash 校验？
4. package 市场是系统内共享，还是允许用户私有牌库？

### Resolved

- `PortableCardPackage` 应独立存储，还是作为 `CardTemplate` 的导出版？→ 作为导出版（见”Package 导出路径”章节）。
- package 的 bundle 文件初期是否只允许文本类白名单？→ 是（见”自定义文件的边界”章节）。

## 推荐下一步

下一步建议先补一份更窄的 v1 规格文档，只回答四件事：

1. manifest 字段最小集合
2. import / instantiate 状态机
3. required / optional 依赖规则
4. bundle 文件白名单与大小限制

等这四件事定住，再考虑 API、后端模型和 UI。
