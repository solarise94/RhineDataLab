# Docker 共享重构(§1.1 + §1.2 + §1.4)+ §3.4 CORS 收尾

> 用法:把本文件整段作为新 agent 会话的第一轮输入。本任务做完后**不要继续做**
> §1.5+ Dockerfile —— 那是下一轮(见末尾"边界")。

---

## 你是谁、在哪

你是 Blueprint RE 的实现 agent。仓库根有 `AGENTS.md`(项目规范,必读)、
`docs/64`(三产物打包方案,本任务来源)、`docs/67`(审计,本任务收尾 §3.4)。

关键纪律:
- 代码是 source of truth。docs 描述与代码实况不符时,以代码为准,并指出来。
- 不为"干净"引入新抽象。优先用现有 pattern。看周围代码怎么写就怎么写。
- 遇到产品决策歧义,先停下来问,列 2-3 个选项让用户选,不要自己拍板。
- 后端测试:`PYTHONPATH=backend .venv/backend/bin/python -m unittest discover -s backend/tests`
- 改了 Pydantic 模型/patch schema 后,跑 `.venv/backend/bin/python scripts/generate_backend_schemas.py`
- 改了 deploy 脚本,用 temp HOME 生成真实 backend.env 做行为测试(AGENTS.md:150)
- **提交纪律:不要自动 commit。完成后告诉用户,由用户决定提交。**

---

## 背景

docs/64 把打包分成三产物(Linux 包 / Docker 镜像 / macOS app),共享一批底层重构。
**macOS 暂不做(用户无开发者账号)**,本任务只做 Docker 的**前置共享重构**,
为下一轮的 Dockerfile 铺路。本任务**不写 Dockerfile**。

---

## 已核实的代码实况(不照搬 docs/64 字面,以代码为准)

- `config.py:81-90` `Settings.data_root` 已经是 `Field(default_factory=...)` 且
  `model_config` 有 `env_prefix="BLUEPRINT_"`(169-171)—— 即 `data_root` **已经能被
  `BLUEPRINT_DATA_ROOT` env 覆盖**。§1.2 的"env 化"基本已完成,只需核查所有写死
  `Path(__file__).resolve().parents[3] / "workspace"` 的地方,确认它们都走 settings。
- `command_worker.py:22-76` `_ensure_bwrap_runtime` 是 bwrap 专属逻辑,没有 renderer 抽象。
- `command_worker.py:239` `getattr(settings, "executor_sandbox_mode", "none") == "bwrap"`
  是硬判断 —— 只认 bwrap。这是 §1.1 要抽象的核心点。
- `executor_sandbox_mode` 默认 `"bwrap"`(config.py:125)。
- `deploy_user_systemd.sh` 渲染 nginx 模板时填默认端口字面量。

---

## 任务 §1.1:沙箱 renderer 抽象(共享重构核心)

**目标**:把 `command_worker.py` 里 bwrap 专属逻辑抽成 renderer 接口,新增 `container`
模式(Docker 用,容器即隔离,不套 bwrap)。`seatbelt` renderer 留空壳(macOS 未来用),
**只定义接口,不实现**。

### 实现要求

1. 定义 renderer 接口(看周围代码的抽象风格 —— 如果项目用 Protocol/ABC 就用,否则用
   duck-typing + 注册函数)。接口至少覆盖:
   - 判断当前 mode 是否需要沙箱(`should_sandbox` 之类)
   - 生成 `sandbox_plan.json`(把现有 bwrap 的 plan 生成逻辑抽出来)
   - 执行 sandboxed 命令(把现有 `_ensure_bwrap_runtime` + bwrap 调用抽出来)
2. 三种 mode:
   - `bwrap`:现有逻辑全部迁过来(行为不变,这是回归红线)
   - `container`:**容器即沙箱**,直接执行命令不套 bwrap。plan 仍生成(用于审计/诊断),
     但不实际套 bubblewrap。AGENTS.md 说 `BLUEPRINT_CONTAINER_ISOLATED=true` 时 executor
     不需要再 bwrap。
   - `seatbelt`:**只定义接口骨架,raise NotImplementedError("seatbelt renderer not yet
     implemented; macOS product deferred")**。不实现,留位置给未来。
3. mode 选择:`executor_sandbox_mode` 接受 `"bwrap" | "container" | "seatbelt" | "none"`。
   未知值 **raise**(不静默 fallback)—— 与 docs/67 的 fail-loud 纪律一致。
4. `BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap`(现状默认)行为**完全不变**。这是回归红线。

### 自主决策边界(这些你可以自己定)

- renderer 的具体抽象形态(Protocol/ABC/注册表)—— 看项目现有 pattern。
- `container` mode 下 `sandbox_plan.json` 写不写、写什么 —— docs/64 说"容器即隔离",
  plan 可用于审计。你看现有 plan 的用途(诊断?reconcile?),决定 container mode 保不保留。
- 空壳 seatbelt 的接口形状 —— 参照 bwrap 接口,留出 `sandbox-exec` profile 生成的位置。

### 回归红线(必须保证)

- **现有 Linux 部署(bwrap)行为零变化**。所有现有 sandbox 相关测试必须全绿。
- `sandbox_mode` 未知值报错,不静默。

---

## 任务 §1.2:data_root env 化核查

**核查实况**:`data_root` 已经能被 `BLUEPRINT_DATA_ROOT` env 覆盖(见上文实况)。

### 实现要求

1. grep 全仓库 `data_root` 的所有使用,确认它们都走 `settings.data_root` 或
   `get_settings().data_root`,而不是直接读 `Path(...) / "workspace"` 或写死路径。
2. Docker 要挂载 `/data`(docs/64:319 `BLUEPRINT_DATA_ROOT=/data`)—— 确认改 env 后
   所有 data 读写都跟着走,没有遗漏的写死路径。
3. 如果发现写死路径,改成走 settings。如果没有,**这个任务就是核查 + 记录,可能零改动**。
   零改动是合法结果,诚实记录即可。

### 自主决策边界

- 发现的写死路径怎么改 —— 看周围代码怎么访问 data_root,沿用。

---

## 任务 §1.4:nginx 端口模板化 + §3.4 CORS 收尾

**目标**:把 nginx.conf 里的端口字面量改成占位符(deploy 脚本填默认值,保持 Linux 现状);
把 `main.py:59-67` 的 CORS 硬编码改成 `settings.frontend_origin`。**这一步收掉 #10
deferred 的 §3.4 CORS**。

### 实现要求

1. **nginx 模板占位符化**(`deploy/nginx/blueprint-re.conf.template`):
   ```
   listen 127.0.0.1:13001;     →  listen __NGINX_LISTEN__;
   proxy_pass http://127.0.0.1:13002;  →  proxy_pass http://127.0.0.1:__FRONTEND_PORT__;
   proxy_pass http://127.0.0.1:18001;  →  proxy_pass http://127.0.0.1:__BACKEND_PORT__;
   ```
   `deploy_user_systemd.sh` / `deploy_release.sh` 渲染时用 sed 填默认值
   (`127.0.0.1:13001` 等),**保持 Linux 现状完全不变**。Docker 的 entrypoint(下一轮)
   会填容器内值。
2. **CORS 硬编码移除**(`backend/app/main.py:59-67`):把
   `"http://127.0.0.1:13001"` / `"http://localhost:13001"` 从字面量改成基于
   `settings.frontend_origin`。核查 `frontend_origin` 的默认值和 env 读取,确保改完后
   Linux 现状(默认 :3000 或当前实际值)行为不变,但端口可通过 env 配置。
3. **§3.4 收尾**:在 docs/67 §3.4 的处置记录里追加"已由 docs/64 §1.4 落地",把 §3.4 从
   deferred 状态更新为 closed。

### 自主决策边界

- `frontend_origin` 该怎么扩展成 CORS origin 列表(单值还是列表?)—— 看现有 main.py
  CORS 中间件怎么用 origin 的,沿用。如果现在是列表且包含字面量,改成包含
  `settings.frontend_origin`。
- 占位符命名(`__NGINX_LISTEN__` 等)—— 用 docs/64:278-280 的命名,或看现有模板风格。

### 回归红线

- **Linux 现状完全不变**:deploy 脚本填默认值后,nginx 行为与今天一致。
- CORS 改动后,现有 frontend(默认 origin)仍能正常跨域访问。

---

## 完成判据

- [ ] §1.1 沙箱抽象:bwrap 行为零变化(现有 sandbox 测试全绿)+ container mode 可用 +
      seatbelt 空壳 + 未知 mode 报错
- [ ] §1.2 data_root 核查:所有使用走 settings(或记录为何处写死需改)
- [ ] §1.4 nginx 占位符 + deploy 脚本填默认值 + CORS 改 settings.frontend_origin
- [ ] §3.4 CORS 在 docs/67 标 closed(由 §1.4 落地)
- [ ] 全量测试绿;改了模型就 regen schema
- [ ] 用 temp HOME 生成真实 backend.env 验证 deploy 脚本仍渲染正确(AGENTS.md:150)
- [ ] docs/64 §1.1/1.2/1.4 追加实施记录
- [ ] docs/67 §3.4 处置记录更新

---

## 边界(不在本任务)

- **不写 Dockerfile / entrypoint.sh / supervisord.conf**(§1.5,下一轮)
- **不做镜像构建脚本 / 配置注入 / Docker 验收**(§1.6-1.8,下一轮)
- **不做 macOS 任何东西**(用户无开发者账号,暂不做)
- **不碰 docs/66**(执行模型与打包正交)
- **不碰 §2.3b**(启动恢复,与 Docker 无关)

---

## 执行顺序与汇报

1. 先读 AGENTS.md + docs/64 §1.1/1.2/1.4 + docs/67 §3.4 处置记录。
2. 核查实况(上文已给,自己再 grep 确认一遍)。
3. §1.1 沙箱抽象 → 跑测试确认 bwrap 回归绿 → §1.2 data_root 核查 → §1.4 nginx + CORS。
4. 全部完成后,给我一份完成报告:
   - 改了什么(文件清单 + 关键改动)
   - 测试结果
   - docs 记录(64 实施记录 + 67 §3.4 更新)
   - 与本 prompt 的实况/判据有无偏差
   - **下一轮 Dockerfile(§1.5)需要的前置是否齐备**(你的改动是否足够支撑下一轮)
5. **不要自动 commit**。

开始前用一句话告诉我你对 §1.1 沙箱抽象的实现计划。
