# Card Library 牌库系统设计方案

## 目标

建立一个系统级的 card 配置文件牌库，让用户可以：

1. 把调试稳定的 card 保存为可复用的"牌"（纯配置，不含脚本）
2. 在牌库管理页面中像扑克牌一样浏览、搜索、管理这些牌
3. 在项目蓝图设计中从牌库选取牌，实例化为项目 card

设计原则是**轻中偏轻度的桌游化**：card 管理和视觉上更有趣、更像实体卡牌，但不改变蓝图设计本身的交互逻辑，不引入出牌/combo 等游戏机制。

## 与现有方案的关系

本方案建立在两份已有设计文档之上：

- `docs/14_manager_execution_speed_and_card_market_plan.md`（Phase 6: Card Market）
- `docs/56_portable_skill_mcp_card_package_plan.md`（Portable Card Package）

关键区别：

| 维度 | 已有方案 | 本方案 |
|------|----------|--------|
| 操作入口 | Manager-only tool call | 用户可直接操作 |
| 存储范围 | 项目内 `_card_templates/` 或 `_system/packages/` | 系统级 `_system/card-library/`，跟人走 |
| 数据格式 | CardTemplate 或 PortableCardPackage manifest | 统一的轻量 blueprint 配置 |
| 脚本携带 | template bundle / package bundle | 不带脚本，可复用脚本走 skill 库 |
| UI | 无 | 牌库管理页 + 项目内牌库侧栏 |

后端的 `CardTemplate` 和 `PortableCardPackage` 模型与服务继续保留，作为 Manager AI 侧的内部复用机制和高级导出格式。本方案新增的是**用户侧的牌库层**，它更轻量、更直观。

## 核心设计决策

### 1. 牌是纯配置，不带脚本

一张牌不包含任何可执行脚本或 bundle 文件。它的本质是一个 executor_context 配置 + 输入输出契约声明。

理由：

- 如果一段脚本有复用价值，它应该成为 skill（通过 skill-creator 创建），而不是塞在 card 的 bundle 里造成版本分裂
- 如果脚本是一次性的，executor 会现场生成，不需要随牌保存
- 不带脚本意味着不需要安全扫描、不需要 bundle 目录、不需要文件数/大小限制

### 2. 牌库是系统级、自包含的

```text
{data_root}/_system/card-library/
  index.json                         # 轻量索引
  blueprints/
    {blueprint_id}/
      blueprint.json                 # 配置文件
      cover.png                      # 可选封面图
```

存储路径与现有 `_system/library/`、`_system/packages/` 同级，统一在 `Settings.data_root` 下，不引入额外存储根路径。

当前项目没有用户管理层，先做系统级。目录结构自包含，将来加用户层时在 `_system/card-library/{user_id}/` 下拆分，不需要改数据格式。

迁移时打包整个 `_system/card-library/` 目录。

### 3. Blueprint 配置格式

```json
{
  "blueprint_id": "singlecell-umap-basic",
  "version": "1.0.0",
  "schema_version": "card_blueprint.v1",
  "title": "单细胞 UMAP 降维",
  "summary": "对单细胞对象执行标准 UMAP 降维并输出图和摘要",
  "tags": ["single-cell", "umap", "visualization"],
  "domain": "bioinformatics",
  "cover_art": null,

  "skills": ["single-cell-plotting"],
  "mcp_servers": ["omicverse"],

  "runtime_requirements": {
    "python": {
      "env_hint": "scanpy-compatible",
      "packages": ["scanpy", "harmonypy"]
    },
    "r": "__system__"
  },

  "inputs_schema": [
    {
      "slot": "expression_object",
      "label": "单细胞对象",
      "accepted_formats": ["h5ad", "rds"],
      "required": true
    }
  ],

  "outputs_schema": [
    {
      "role": "umap_figure",
      "label": "UMAP 降维图",
      "artifact_class": "figure",
      "accepted_formats": ["svg", "png"],
      "preferred_format": "svg",
      "required": true
    }
  ],

  "parameters": [
    {
      "name": "color_by",
      "type": "string",
      "required": false,
      "default": null
    }
  ],

  "instruction_blocks": [
    "输出文件写到 run-local 目录",
    "不要修改项目图文件"
  ],

  "provenance": {
    "source_card_id": null,
    "source_project_id": null,
    "created_at": "2026-06-13T00:00:00Z",
    "created_by": "user",
    "last_used_at": null,
    "use_count": 0
  }
}
```

字段说明：

- `skills` / `mcp_servers`：引用系统能力库的 id，不内嵌能力本体
- `runtime_requirements`：声明需要什么能力（包、环境特征），不硬绑具体 conda_env 名
- `inputs_schema`：槽位声明（角色 + 格式），不绑具体 asset_id
- `outputs_schema`：同上
- `instruction_blocks`：通用指令，不含项目特定内容
- `cover_art`：nullable string，上传封面后写入实际文件名（`cover.png` / `cover.jpeg` / `cover.webp`），删除封面时置 null
- `provenance`：来源追踪，`source_card_id` 和 `source_project_id` 在存牌时脱敏置 null

### 4. 脚本 vs Skill 的边界

```text
可复用脚本   → skill-creator → 进入 skill 库 → card 通过 skills 引用
一次性脚本   → 不保存，executor 现场生成
配置模板     → card 的 parameters 字段
参数提示     → card 的 instruction_blocks
```

如果用户在存牌时发现某段脚本值得保留，正确路径是用 skill-creator 把它做成 skill，然后在牌的 `skills` 字段里引用。牌本身不做脚本容器。

### 5. PortableCardPackage 的未来角色：数据资产随行

Card Library 的牌是纯配置，不带文件。但实际分析场景中，很多 card 依赖特定的数据资产：

- GTF 注释文件（基因结构注释）
- 基因列表（marker genes、pathway gene sets）
- 参考数据库（细胞类型数据库、通路数据库）
- 预训练模型或参考索引

这些数据资产不是脚本，不适合做成 skill；但它们也不是"用户每次都会自己准备"的输入数据——它们是方法的一部分，跟牌走才有意义。

这是 `PortableCardPackage` 的核心价值：**当一张牌需要携带数据资产迁移时，升级为 package 格式**。

```text
Card Blueprint（牌库里的牌）
  = 纯配置 + skill/MCP 引用 + runtime requirement
  → 适合：不依赖特定数据文件的分析方法牌

PortableCardPackage（导出/分发格式）
  = 配置 + 数据资产 bundle
  → 适合：需要携带 GTF、基因列表、数据库等数据附件的牌
  → 迁移时打包整个 package（配置 + 数据文件）
```

设计边界：

- 牌库日常存储用轻量的 Card Blueprint 格式
- 当用户需要把一张牌连带数据资产一起导出或迁移时，从 blueprint 生成 PortableCardPackage
- Package 的 bundle 目录放数据文件（GTF、CSV、JSON 等），不放可执行脚本
- 数据文件仍然有大小和数量限制（防止牌变成数据集分发工具）

这也明确了牌库里 `inputs_schema` 和 package bundle 的分工：

- `inputs_schema` 声明的是"用户必须提供的分析输入"（如单细胞表达矩阵）
- package bundle 携带的是"方法自带的参考数据"（如 GTF、基因列表）

两者不能混淆：bundle 里的数据不应出现在 inputs_schema 中，inputs_schema 的输入也不应由 bundle 提供。

## 存牌流程

### 触发入口

用户在 card 详情页或右键菜单中点击"存入牌库"。

### 自动化流程

```text
用户点击"存入牌库"
  ↓
Step 1: 系统自动提取 + 规则脱敏
  - 从 card.executor_context 提取 skills、mcp_servers、instruction_blocks
  - 从 card.inputs 生成 inputs_schema
    （CardAssetRef 只有 label/asset_id/status，没有格式字段。
    accepted_formats 从绑定 asset 的文件扩展名推断；
    无法推断时 accepted_formats=[]，实例化 UI 不做格式过滤，只提示用户确认。）
  - 从 card.outputs 生成 outputs_schema
  - 从 card.executor_context.runtime_bindings 生成 runtime_requirements
    （注意：RuntimeBindings 只有 conda_env/r_env 名称，没有包清单。
    因此 runtime_requirements.packages 不能自动推导，必须由用户或 AI review 显式声明。
    env_hint 可从 conda_env 名称提取作为提示，但不作为最终约束。）
  - 脱敏处理：去除 source_card_id、source_project_id、绝对路径、样本名
  ↓
Step 2: Manager AI 轻量 review
  - 检查 instruction_blocks 是否有项目泄漏（项目名、样本名、特定数据描述）
  - 检查 title/summary 是否够通用
  - 如果需要泛化，生成修改建议
  ↓
Step 3: 结果分支
  - review 通过 → 直接保存，toast "已存入牌库"
  - review 发现问题 → 弹简洁修改建议框
    - 用户点"接受建议" → 应用修改后保存
    - 用户点"跳过" → 直接保存原始版本
  - AI review 不可用（provider credentials 缺失或 manager-agent 降级）→
    只执行规则脱敏，直接保存，toast 提示"已存入牌库（未进行 AI 泛化检查）"
```

### 脱敏规则

| 字段 | 脱敏方式 |
|------|----------|
| title | 保留，AI 检查是否通用 |
| summary | 保留，AI 检查是否通用 |
| skills | 直接保留（引用 id，无泄漏风险） |
| mcp_servers | 直接保留（引用 id） |
| runtime_bindings | 转为 requirement：去掉 conda_env 名，env_hint 从环境名提取；packages 不能自动推导，由用户/AI 显式声明 |
| inputs | 转为槽位声明（去掉 asset_id，保留格式和角色） |
| outputs | 保留 schema，去掉具体 asset 引用 |
| instruction_blocks | AI 检查，提示去除项目特定内容 |
| parameters.default | 置为 null（防止数据集特定值泄漏，如 "cell_type"） |
| linked_modules/runs/assets | 全部丢弃 |
| key_findings/manager_review | 全部丢弃 |

规则脱敏（Step 1）覆盖大部分场景：正则去掉 asset_id 格式、绝对路径、`/home/*/` 模式。AI review（Step 2）只兜底语义级泄漏。

### 用户体验目标

主路径是**一键存牌**：点一下 → toast "已存入" → 完事。

只有 AI 发现明显问题时才打断，且用户可以选择跳过。用户一般不会仔细看确认框，所以系统自动提取的质量必须足够高。

## UI 改造范围

### 原则

- 改造现有 ModuleCard 视觉风格，不加新皮肤、不做双套 UI
- 蓝图设计区的交互逻辑不变（拖拽、连线、step 分组等保持原样）
- 桌游化只体现在 card 外观和牌库管理体验上

### 1. ModuleCard 视觉优化

保留现有三页结构（Cover / Result / Files），改造外观：

- 更大的圆角、微妙的厚度感（阴影 + 边框）
- 封面页支持自定义封面图（来自牌库的 cover.png）
- 状态色带保留（对应现有 CardStatusBadge 的 11 种状态）
- 牌面信息更紧凑：标题 + 一行摘要 + skill/MCP 图标 + runtime 标签

用户可以为自己保存的牌提供封面图。系统也可以根据 tags/domain 提供默认封面。

### 2. /card-library 牌库管理页

新增前端路由 `frontend/app/card-library/page.tsx`，扑克牌式网格布局：

导航入口：在现有顶部导航栏（首页 / 项目列表）中增加"牌库"入口。

空态设计：牌库为空时显示引导文案（"还没有牌。完成一个分析项目后，可以把稳定的 card 存入牌库。"）和一个示例牌的预览卡片。

```text
┌─────────────────────────────────────────────────────┐
│  Card Library                          [搜索] [筛选] │
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ 封面图    │  │ 封面图    │  │ 封面图    │           │
│  │          │  │          │  │          │           │
│  │ 标题     │  │ 标题     │  │ 标题     │           │
│  │ 一行摘要  │  │ 一行摘要  │  │ 一行摘要  │           │
│  │ [tag][tag]│  │ [tag][tag]│  │ [tag][tag]│           │
│  │ 🛠 skill │  │ 🛠 skill │  │ 🛠 skill │           │
│  │ 🐍 scanpy│  │ 🐍 omic  │  │ 🐍 base  │           │
│  └──────────┘  └──────────┘  └──────────┘           │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │   ...    │  │   ...    │  │   ...    │           │
│  └──────────┘  └──────────┘  └──────────┘           │
│                                                      │
└─────────────────────────────────────────────────────┘
```

功能：

- 网格展示所有牌，每张牌显示封面图、标题、摘要、tags、skill/MCP 图标、runtime 标签
- 搜索框：按 title/summary/tags 搜索
- 筛选：按 domain、runtime、skill 筛选
- 点击牌 → 展开详情（完整配置、输入输出 schema、provenance）
- 操作：删除、编辑元数据（title/summary/tags）
- 导入/导出为 JSON（P2）
- 使用次数和最近使用时间展示

### 3. 项目内牌库侧栏

现有 `CapabilitiesPanel` 是三个面板（SkillHubPanel / McpHubPanel / CapabilityInstallPanel）顺序堆叠，不是 tab 容器。需要先改造为 tab/segmented panel，再增加 Deck 标签页。

改造后：

```text
┌──────────────────────────────┐
│ Skills │ MCP │ Install │ Deck │
├──────────────────────────────┤
│ [搜索牌库...]                 │
│                              │
│ ┌──────────────────────────┐ │
│ │ 单细胞 UMAP 降维          │ │
│ │ scanpy │ 🛠 1 📡 1       │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ 差异表达分析              │ │
│ │ R/DESeq2 │ 🛠 0 📡 1     │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ 通路富集分析              │ │
│ │ Python/R │ 🛠 1 📡 0     │ │
│ └──────────────────────────┘ │
│                              │
└──────────────────────────────┘
```

实施前提：

- P1 阶段先改造 CapabilitiesPanel 为 tab 容器（保留现有三个面板为 tab 内容）
- P1 阶段新增 BlueprintDeckPanel 作为第四个 tab

交互：

- 点击一张牌 → 弹出实例化预览面板：
  - 输入槽位绑定（从项目资产中选择）
  - Runtime 选择（根据牌声明的 requirement + 项目可用 runtime 匹配）
  - 参数填写
- 确认 → 创建 card 到当前项目蓝图，走现有 card 生命周期
- 已有项目 card 可右键"存入牌库"

### 4. 蓝图区视觉微调

蓝图设计区（CardStream）的交互逻辑不变，但 card 外观跟随 ModuleCard 的视觉优化。

可选增强：

- 从牌库侧栏拖拽牌到蓝图区时，加一个轻量的放置动画
- 从牌库实例化的 card 在封面角上显示一个小标记（表示"来自牌库"）

## 后端 API

### 新增端点

所有牌库 API 放在 `/api/card-library/` 下。实例化需要项目上下文，放在项目路由下：

```text
GET    /api/card-library                                          # 列表（返回 index.json）
GET    /api/card-library/search?query=&tags=&domain=&runtime=      # 搜索（GET + query params，与 /library/ 风格一致）
GET    /api/card-library/{blueprint_id}                            # 详情（返回 blueprint.json）
POST   /api/card-library                                          # 从项目 card 创建牌（body: {project_id, card_id}）
PUT    /api/card-library/{blueprint_id}                            # 编辑元数据
DELETE /api/card-library/{blueprint_id}                            # 删除
POST   /api/card-library/import                                   # 导入 blueprint JSON（body: blueprint 对象）
GET    /api/card-library/{blueprint_id}/export                     # 导出为 JSON
GET    /api/card-library/{blueprint_id}/cover                       # 获取封面图
PUT    /api/card-library/{blueprint_id}/cover                       # 上传封面图

# 实例化需要项目上下文，走项目路由：
POST   /api/projects/{project_id}/card-library/{blueprint_id}/instantiate
```

端点职责单一：`POST /api/card-library` 只接受 `{project_id, card_id}` 从项目 card 创建牌；`POST /api/card-library/import` 只接受完整 blueprint JSON 对象用于导入。两种来源不共用端点。

### 新增服务

```text
backend/app/services/card_library_service.py
```

职责：

- 牌库 CRUD（读写 `{data_root}/_system/card-library/`）
- 从 card 提取 + 脱敏 + 保存
- 搜索与筛选（内存索引，和 library_registry_service 类似的模式）
- 存牌时做格式校验（skill/MCP id 是否合法字符串、字段完整性），不做可用性校验
- 索引读写使用 atomic_write_json + 服务级 RLock
- 导入/导出 JSON

实例化职责（需要项目上下文，放在项目路由下）：

- 从 blueprint 配置创建项目 card，做输入绑定、runtime 解析
- 通过项目级 `LibraryRegistryService` 校验 skill/MCP 是否真实可用
- 通过 `RuntimeDependencyResolverService` 解析 runtime requirement

### 与现有服务的关系

```text
CardLibraryService（新增）
  ├─ 调用 GraphStore 读取源 card 数据（存牌时）
  ├─ 存牌时：仅做格式校验，不调用项目级服务
  └─ 索引读写使用 atomic_write_json + threading.RLock

实例化路径（项目级）：
  ├─ 调用 GraphStore 写入新项目 card
  ├─ 调用项目级 LibraryRegistryService 校验 skill/MCP id 可用性
  └─ 调用 RuntimeDependencyResolverService 解析 runtime requirement
```

`LibraryRegistryService` 是面向项目数据的，需要 per-project `data_root` 构造。牌库作为系统级组件，存牌时不应依赖它。真正的 skill/MCP 可用性校验推迟到实例化时，由项目级服务完成。

```text
ManagerBlueprintTools（现有）
  ├─ save_card_template → 继续作为 Manager 侧内部模板机制
  └─ instantiate_card_template → 继续作为 Manager 侧内部复用

PackageService（现有）
  └─ 继续作为高级导出格式，不与牌库直接耦合
```

## 实例化流程

用户从牌库选牌并实例化时：

```text
1. 读取 blueprint.json
2. 用户绑定输入资产（slot → project asset_id）
3. 用户选择 runtime（牌的 requirement → 项目可用 runtime 匹配推荐）
4. 用户填写参数（如有）
5. 系统创建 Card 对象：
   - card_type = "module"
   - status = "proposed"（与现有 card 生命周期一致）
   - executor_context 从 blueprint 的 skills/mcp/runtime/instruction_blocks 组装
   - inputs 从用户绑定的资产填充
   - outputs 从 blueprint 的 outputs_schema 生成（字段直接复用 OutputContractBase：role/label/artifact_class/accepted_formats/preferred_format；实例化时丢弃 asset_id/status，由项目运行时填充）
6. Card 写入项目 graph
7. 更新 blueprint 的 provenance.use_count 和 last_used_at
```

### Parameters 落脚点

blueprint 的 `parameters` 数组在实例化时需要落地到 Card 模型中。现有 Card 和 ExecutorContext 没有 parameters 字段，因此采用注入策略：

- 用户在实例化时填写的参数值，被格式化为 instruction_block 追加到 `executor_context.instruction_blocks` 末尾
- 格式约定：每条参数生成一个 block，形如 `Parameter {name} = {value}`
- 如果参数未填写且有非 null default，使用 default 值
- 如果参数 required=true 且未填写，实例化阻断并提示用户

**Parameters 脱敏边界：**

- 从项目 card 存牌时：`default` 一律置 null（防止数据集特定值如 "cell_type" 泄漏）
- 导入 JSON 或手动创建的 blueprint：允许保留 `default`（用户对自己的牌库负责）
- 参数值限制：只允许 string / number / boolean，不允许对象和数组（避免注入复杂结构）
- 参数值过滤：正则检测路径模式（`/home/`、`C:\`）、疑似 secret（含 `key`/`token`/`password` 的值），命中时阻断并提示

示例：blueprint 声明 `color_by` 参数，用户填写 `cell_type`，则追加 instruction block：`Parameter color_by = cell_type`。

这样做的好处是不需要改动 Card 数据模型，parameters 自然融入 executor 的执行上下文中。

实例化后的 card 进入现有项目生命周期（proposed → planned → running → reviewing → accepted），与手动创建的 card 完全一致。

## Runtime 声明格式

牌的 `runtime_requirements` 不硬绑具体环境名，而是声明需求：

```json
{
  "python": {
    "env_hint": "scanpy-compatible",
    "packages": ["scanpy>=1.9", "harmonypy"]
  },
  "r": "__system__"
}
```

实例化时的 runtime 匹配策略（P0 范围）：

- 遍历项目已知的 conda_env 列表，按 env_hint 做名称模糊匹配推荐
- 调用 `RuntimeDependencyResolverService` 对声明的 packages 做可安装性预检
- 预检通过 → 推荐该 env；预检无法验证 → 给 warning，不阻断实例化
- 如果声明了 packages 但 runtime 为 `__system__`，要求用户选择具体 runtime 或显式跳过验证
- P0 不做完整的"已安装包盘点"，只做 resolver 级别的声明包可安装性判断

`__system__` 表示不要求显式绑定，使用系统默认。

## 索引格式

```json
{
  "schema_version": "card_library_index.v1",
  "entries": [
    {
      "blueprint_id": "singlecell-umap-basic",
      "title": "单细胞 UMAP 降维",
      "summary": "对单细胞对象执行标准 UMAP 降维并输出图和摘要",
      "tags": ["single-cell", "umap", "visualization"],
      "domain": "bioinformatics",
      "skills": ["single-cell-plotting"],
      "mcp_servers": ["omicverse"],
      "runtime_hints": ["scanpy-compatible"],
      "use_count": 3,
      "last_used_at": "2026-06-12T10:00:00Z",
      "created_at": "2026-06-10T00:00:00Z"
    }
  ]
}
```

索引只包含轻量字段，列表和搜索只读 index.json。详情页才读完整 blueprint.json。

## 迁移性

整个牌库是自包含目录：

```text
_system/card-library/
  index.json
  blueprints/
    */blueprint.json    # 纯配置，无绝对路径
    */cover.png         # 可选封面图
```

迁移方式：

- 导出：打包 `_system/card-library/` 目录为 zip/tar
- 导入：解压到目标机器的 `{data_root}/_system/card-library/`
- 合并策略：按 blueprint_id 去重；冲突时提示用户选择（保留/替换/重命名），不自动覆盖

牌的配置中不包含任何机器特定信息（无绝对路径、无 host-specific runtime 名、无 asset_id），因此跨机器迁移是安全的。

skill/MCP 的引用是 id 级别的，目标机器需要有对应的 skill/MCP 安装在系统能力库中。实例化时系统会做兼容性检查并提示缺失项。

## Blueprint ID 生成策略

`blueprint_id` 由系统自动生成，用户只编辑 title/summary/tags。生成规则：

- 基于 title 的 slug 化 + 短随机后缀，例如 `singlecell-umap-a3f2`
- 保证在牌库内唯一
- 保存时如检测到冲突，自动追加额外后缀

用户不可手动指定 blueprint_id，避免命名冲突和格式不一致。

## 并发安全

牌库的 `index.json` 可能面临并发写入（多窗口操作、快速连续存牌/删除）。

现有 `atomic_write_json`（`backend/app/services/utils.py`）只做原子替换（mkstemp + os.replace），不含文件锁。因此 `CardLibraryService` 需要自己的并发控制：

- 服务级 `threading.RLock` 保护读-改-写序列（单进程内安全）
- 写入仍使用 `atomic_write_json` 保证文件原子性（防止写到一半崩溃导致索引损坏）
- 索引更新和 blueprint 目录操作在同一个锁范围内，保证索引与目录一致
- 如果未来需要多进程安全，再补 `fcntl.flock`（当前 uvicorn 单 worker 模型下 RLock 足够）

## 封面图安全

`PUT /api/card-library/{blueprint_id}/cover` 上传封面图时的限制：

- 允许格式：PNG、JPEG、WebP
- 文件大小上限：2 MB
- 图片尺寸：上传时自动缩放到最大 800x600，保持比例
- 文件名固定为 `cover.{ext}`，不允许路径穿越
- 不支持 SVG（避免 XSS 风险）

## 数据模型校验

后端新增 Pydantic model `CardBlueprint`（定义在 `backend/app/models/card_blueprint.py`），用于：

- 保存和导入时的 JSON 校验（替代手写 JSON 检查）
- API 请求/响应的序列化
- `schema_version` 字段支持未来 v1 → v2 迁移（校验时根据 version 路由到对应 model）

与现有 `Card`、`PackageManifest`、`CardTemplate` 等 Pydantic model 模式一致。

## 实施优先级

### P0 — 最小可用

1. Pydantic model `CardBlueprint`（`backend/app/models/card_blueprint.py`）
2. `CardLibraryService`：CRUD + 从 card 提取保存 + 索引管理 + 原子写
3. `/card-library` 牌库管理页：网格展示 + 搜索 + 删除
4. 存牌入口：card 详情页"存入牌库"按钮 + 自动脱敏
5. 实例化 API：从牌库创建项目 card（`POST /api/projects/{project_id}/card-library/{blueprint_id}/instantiate`）

### P1 — 体验增强

1. CapabilitiesPanel 改造为 tab 容器（现有三面板 + BlueprintDeck 第四个 tab）
2. ModuleCard 视觉优化（圆角/厚度/封面图）
3. 封面图上传与默认封面
4. Manager AI review 兜底（含降级路径）
5. 顶部导航增加"牌库"入口

### P2 — 后续

1. 导入/导出 JSON（迁移工具）
2. Manager 自动从 accepted card 提炼高质量牌
3. 牌的使用统计与推荐
4. 与 CardTemplate / PortableCardPackage 的互通（从 template 导出为牌、从牌生成 package）

## 不在本方案范围内

- 在线牌库 / 多人共享 / 评分推荐
- 出牌/combo/连击等游戏机制
- 牌库内的牌携带可执行脚本（脚本走 skill 库）
- 牌库内的牌携带数据资产（数据资产走 PortableCardPackage 导出格式）
- 牌间依赖解析
- 自动脱敏 AI（初期用规则 + AI 轻量 review）

## 测试与验收清单

### 后端

| 场景 | 预期 |
|------|------|
| CardBlueprint Pydantic 校验 | 缺必填字段（title/schema_version/blueprint_id）时抛 ValidationError；inputs_schema 允许空数组（支持无输入的汇总/报告牌） |
| CRUD 原子写 | 连续快速存牌+删除，index.json 始终与 blueprints/ 目录一致 |
| blueprint_id 冲突 | 同 title 存两次，第二次自动追加后缀，两个牌共存 |
| 从 card 保存 + 脱敏 | source_card_id/source_project_id 为 null，无绝对路径，parameters.default 为 null |
| runtime packages 未声明 | 保存成功，runtime_requirements.packages=[]，实例化时给 warning |
| 实例化写入 card | 新 card status="proposed"，inputs 绑定到项目 asset，outputs 无 asset_id |
| 参数注入 instruction_blocks | 实例化后 card.executor_context.instruction_blocks 末尾有 "Parameter x = y" |
| skill/MCP 本地不存在 | 存牌成功（格式校验通过），实例化时返回 warning 列出缺失项 |
| runtime `__system__` + 声明 packages | 实例化时要求用户选具体 runtime 或跳过验证 |
| cover 上传拒绝 SVG | PUT cover 传 SVG 文件时返回 400 |
| cover 超大文件 | 超过 2MB 时返回 413 |
| AI review 降级 | provider credentials 缺失时保存成功，toast 提示"未进行 AI 泛化检查" |

### 前端

| 场景 | 预期 |
|------|------|
| 牌库页空态 | 显示引导文案和示例预览 |
| 牌库页网格 | 封面图+标题+tags+skill 图标+runtime 标签正确渲染 |
| 搜索与筛选 | 按 query/tags/domain 过滤结果正确 |
| 存牌入口 | card 详情页点"存入牌库"→ toast 通知 |
| 实例化流程 | 侧栏选牌→输入绑定→runtime 选择→参数填写→card 出现在蓝图 |
| `npm run build` | 无 TypeScript 错误 |
