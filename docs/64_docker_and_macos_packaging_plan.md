# 64. 三产物打包方案：Linux / Docker / macOS

Status: design + implementation plan (no code written yet). 已按 `docs/65`
工作流 IDE 定位修订：从“两阶段（Docker→macOS）”改为“三产物并行，共享核心”。

Date: 2026-06-21

Related:

- `docs/65_product_positioning_workflow_ide.md`（**定位锚**：工作流 IDE。
  本文档的打包形态、沙箱威胁模型均以此为准）
- `docs/66_execution_model_and_dependency_chain_redesign.md`（执行模型与依赖链，
  与打包正交，可并行实现）
- `docs/60_bundled_mamba_r_runtime_plan.md`（离线 micromamba + 运行时缓存模式）
- `docs/62_default_python_runtime_and_bundled_omicverse_plan.md`（默认运行时绑定）
- `scripts/build_release_bundle.sh`、`scripts/deploy_release.sh`、
  `scripts/deploy_user_systemd.sh`（现有 Linux 打包/部署链）
- `backend/app/workers/command_worker.py`（bwrap 沙箱唯一实现）
- `backend/app/core/config.py`（`Settings` + `data_root`）
- `AGENTS.md`（`Executor Runtime Notes`、`Path And Privacy Rules`）

## TL;DR

一个仓库、共享核心（`backend/` + `frontend/` + `manager-agent/`）、**三个构建产物**：

| 产物 | 形态 | 进程编排 | 沙箱 renderer | 定位 |
|------|------|----------|---------------|------|
| Linux 包 | 自解压安装器 → systemd | systemd --user | bwrap | 现有，服务器/高级用户 |
| Docker 镜像 | 单镜像单容器 | supervisord | container（容器即隔离） | 一键部署，远程/CI |
| macOS app | Electron .app | Electron 主进程 | seatbelt | 桌面 IDE，交互式首选 |

三个产物**并行**，不是先后阶段。它们共享同一批底层重构——**沙箱后端抽象**、
**进程编排层**、**运行时/状态目录解耦**、**端口/env 模板化**。这些共享重构做完，
三个产物各自独立构建，互不阻塞。

工程排期上 Docker 先落地（验证沙箱抽象 + 进程编排），macOS 紧随——但这是**工程风险
驱动的顺序**（macOS universal2 运行时是最大不确定性），不是产品重要性排序。按
`docs/65` 的 IDE 定位，macOS 桌面是与 Linux/Docker 并列的一等产物，不是“第二阶段”。

### 为什么不分叉成两个仓库

定位是工作流 IDE 后，这个判断更稳了（类比 RStudio Desktop / RStudio Server 是同一
代码库两个产物）。代码层面没有“只有 server 才有”或“只有 desktop 才有”的分叉点：

- backend 运行时代码**零 `systemctl` 调用**（systemd 只活在 `deploy/` 和 `scripts/`，
  是部署依赖不是运行时依赖）
- 三个运行时进程（backend/frontend/manager-agent）在所有产物里完全相同
- executor/worker、manager-auto、依赖链逻辑三产物共享，无 server-only / desktop-only

强行分叉会带来双倍 bug 修复成本和分叉漂移风险，换不到任何代码层面的清晰度。保持
monorepo + 构建产物维度。真到了“server 要多租户/远程、desktop 坚持单用户”那天再分叉
也不迟——从 monorepo split 出去比合并两个仓库容易。

---

## 用户已确认的关键决策

| 维度 | 决策 |
|------|------|
| 仓库结构 | **单仓库三产物**，不分叉（见 TL;DR 论证） |
| Docker 沙箱 | **容器即沙箱**——去掉 bwrap，靠容器本身隔离；新增 `container` 沙箱模式 |
| Docker 镜像形态 | **单镜像单容器**，进程管家拉起 backend + frontend + manager-agent |
| Docker 运行时 | **镜像内置完整**：Python 3.13 + Node 22 + micromamba + omicverse + R |
| Docker 配置输入 | **环境变量 / 挂载 `.env`**，与现有 `.env` 机制一致 |
| macOS 沙箱 | **Apple sandbox-exec（Seatbelt）**，翻译 `sandbox_plan.json` |
| macOS 形态 | **Electron 壳 + 进程管家**，全捆绑运行时 |
| macOS 架构 | **universal2**（arm64 + x86_64） |
| macOS 运行时 | **全捆绑**：App 内置 micromamba + Python + Node，首次启动安装到用户目录 |
| 本地端口 | **可配置**，默认沿用 13001/13002/18001/18002，env 可覆盖 |
| 沙箱代码组织 | **新建 `backend/app/workers/sandbox/` 渲染器目录**，每个后端一个 renderer |
| 工程顺序 | Docker 先落地（验证共享重构），macOS 紧随；**非产品重要性排序** |

### 沙箱威胁模型（按 `docs/65` IDE 定位重述）

沙箱隔离的对象是：**执行器节点（LLM coding agent）生成并运行的用户请求分析代码
（Python/R）对文件系统/进程的越权**，不是隔离用户本身。

这个定位约束了沙箱选型：

- **交互式场景要求沙箱启动开销小**——用户点一个 card 重跑，期望秒级响应。bwrap 的
  namespace 创建是毫秒级，合适；OCI runtime（runc/crun）拉起开销大，对秒级交互任务
  过重，**不采用**。
- 三产物各用各的轻量 renderer（bwrap / container / seatbelt），不追求“全平台同一沙箱
  二进制”。容器边界、bwrap namespace、seatbelt profile 是三种隔离原语，但都满足
  “轻量 + 隔离 FS/进程”这个共同契约。
- `data_mount` 输入的硬门控保留：只有 `mode == "none"` 才拒绝 data_mount 运行，
  `bwrap` / `container` / `seatbelt` 均放行（各自提供隔离）。

---

## 现状摸底（为什么要做这些调整）

### 现有 Linux 部署链路

```
build_release_bundle.sh  →  tar payload（wheels + frontend-standalone + manager-agent + runtime/）
        ↓
build_self_extracting_installer.sh  →  rhinedatalab-<v>-linux-x86_64.sh（自解压）
        ↓
install.sh  →  deploy_release.sh  →  systemctl --user 四个 unit
        ↓
backend(18001) + manager-agent(18002) + frontend(13002) + nginx(13001)
```

**强 Linux 依赖点（Docker/macOS 都要绕开或替换）：**

1. **systemd --user**：`deploy_user_systemd.sh` / `deploy_release.sh` 全程用
   `systemctl --user` 管理四个服务。Docker 无 systemd，macOS 用 launchd。
2. **bubblewrap（bwrap）**：`command_worker.py` 是唯一沙箱实现，Linux user namespace
   专属。Docker 里要 `--privileged`，macOS 上根本不存在。
3. **manylinux x86_64 wheel 锁定**：`build_release_bundle.sh` 把 wheel 下载锁死在
   `manylinux_2_17_x86_64 / cp313`，macOS 无法复用。
4. **micromamba linux-64**：捆绑的 micromamba 二进制是 linux-64 平台。
5. **nginx 硬编码端口**：`deploy/nginx/blueprint-re.conf.template` 里 13001/13002/18001
   是字面量，不是占位符。
6. **apt 安装依赖**：`deploy_user_systemd.sh` 假设 apt（`apt-get install bubblewrap
   python3-venv nodejs nginx`）。
7. **`/home/` 路径正则**：`diagnostic_bundle_service.py` 等用 `/home/[^/\s]+` 做脱敏，
   macOS 是 `/Users/`，会漏脱敏。
8. **`data_root` 不可 env 覆盖**：`config.py:84` 写死
   `Path(__file__).resolve().parents[3] / "workspace"`，Docker 要挂卷、macOS 要放
   `~/Library/Application Support/`，都得先把它做成可配置。

### 服务拓扑与耦合

```
browser → nginx :13001
            ├─ /upload-api/* → backend :18001/api/*
            └─ /*           → frontend :13002/*
backend → manager-agent :18002 （HTTP /chat-stream SSE）
manager → backend :18001/api/internal/... （HTTP + bearer token，回调闭环）
```

- 三个服务的 host/port **都 env 可配**，唯独 nginx 的字面量端口不是。
- 无数据库，所有状态是 `data_root` 下的 JSON 文件。
- 长任务（executor 子进程、runtime 依赖安装、manager auto wake）都在 backend 进程内
  的守护线程里跑，无 cron / launchd / 外部 worker。

---

## 产物一：Docker 一键部署

### 目标

```bash
docker run -d \
  -p 13001:13001 \
  -v blueprint-data:/data \
  -e BLUEPRINT_DEEPSEEK_API_KEY=sk-xxx \
  --name blueprint ghcr.io/solarise94/rhinedatalab:0.5.1
# 浏览器打开 http://localhost:13001
```

一条命令，开箱即用，状态持久化到 named volume。

### 1.1 沙箱抽象（共享重构，三产物复用）

**问题**：`command_worker.py` 把 bwrap 写死了，且 `data_mount` 输入有硬门控——
"没有 bwrap 就拒绝跑"。

**改造**：新建 `backend/app/workers/sandbox/`，把沙箱渲染从 `command_worker.py`
抽出来，按 `executor_sandbox_mode` 分发：

```
backend/app/workers/sandbox/
  __init__.py
  base.py            # SandboxRenderer 抽象基类
  plan.py            # SandboxPlan 数据结构（从现有 sandbox_plan.json 提炼）
  bwrap.py           # 现 command_worker._wrap_with_bwrap 搬过来
  container.py       # 新增：容器即沙箱，no-op renderer（不额外包裹）
  seatbelt.py        # 产物二：macOS sandbox-exec
```

`SandboxPlan` 复用现有 `sandbox_plan.json` 字段（已是渲染器无关的）：
`mode`、`readonly_binds`、`writable_binds`、`masked_paths`、`env_keys`、
`run_local_dirs`。

`container` renderer 的语义：**不包裹命令**，直接返回原 argv，但在
`SandboxPlan` 里记录"隔离由容器边界提供"。`data_mount` 门控改为识别
`mode in ("bwrap","container","seatbelt")` 即放行，只有 `mode == "none"` 才硬拒。

`command_worker._should_use_bwrap` 改名 `_should_sandbox`，分发逻辑：

```python
def resolve_renderer(settings) -> SandboxRenderer:
    mode = settings.executor_sandbox_mode  # "bwrap" | "container" | "seatbelt" | "none"
    if mode == "bwrap":    return BwrapRenderer()
    if mode == "container":return ContainerRenderer()
    if mode == "seatbelt": return SeatbeltRenderer()
    return NoneRenderer()
```

`_ensure_bwrap_runtime` 的 smoke test 只在 `mode == "bwrap"` 时跑；
`container` 模式不做 smoke（容器边界已保证）。

**`Settings` 新增**（`config.py`）：`executor_sandbox_mode` 仍是唯一开关，不新增字段，
只是合法值从隐式的 `"bwrap"` 扩到枚举校验。新增 `BLUEPRINT_CONTAINER_ISOLATED`
（运行期自检用：容器内启动时设 `true`，renderer 据此确认自己在受隔离环境）。

### 1.2 `data_root` 可配置

**改造**（`config.py:84`）：

```python
data_root: Path = Field(
    default_factory=lambda: Path(__file__).resolve().parents[3] / "workspace"
)
```

改为：

```python
data_root: Path = Field(
    default_factory=lambda: Path(os.environ.get("BLUEPRINT_DATA_ROOT",
        str(Path(__file__).resolve().parents[3] / "workspace")))
)
```

（`env_prefix="BLUEPRINT_"` 已经会把 `BLUEPRINT_DATA_ROOT` 映射进来，但显式写出
默认值更清晰；实际只需确认 `Field` 能被 env 覆盖——pydantic-settings 默认就能。）

Docker 里设 `BLUEPRINT_DATA_ROOT=/data`，挂卷。

### 1.3 进程编排层（共享重构，三产物复用）

systemd 干的事：拉起四个进程、按依赖顺序、退出重启、统一日志。Docker 单容器内
需要一个等价物。**用 `supervisord`**（Python 生态、配置简单、与 backend 同语言）。

新建 `deploy/docker/supervisord.conf`：

```ini
[supervisord]
nodaemon=true
user=blueprint
pidfile=/tmp/supervisord.pid
logfile=/dev/stdout
logfile_maxbytes=0

[program:manager-agent]
command=/opt/runtime/node/bin/node /app/manager-agent/src/server.js
directory=/app/manager-agent
autorestart=true
priority=10

[program:backend]
command=/opt/runtime/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18001
directory=/app/backend
autorestart=true
priority=20
startsecs=3

[program:frontend]
command=/opt/runtime/node/bin/node server.js
directory=/app/frontend-standalone/frontend
environment=HOSTNAME="127.0.0.1",PORT="13002"
autorestart=true
priority=30
startsecs=2
```

（顺序 manager → backend → frontend，与现 systemd `After=` 链一致。）

**端口策略**：默认沿用 13001/13002/18001/18002，但全部走 env。supervisord 配置里
用 `%(ENV_...)` 或启动脚本读 env 渲染。镜像对外只暴露 `BLUEPRINT_GATEWAY_PORT`
（默认 13001）。

**nginx 取舍**：单容器里保留 nginx 做 `/upload-api` 大文件流式转发（无缓冲、
36000s 超时）是有价值的，否则前端 Next.js rewrite 走 backend 会对上传有缓冲问题。
所以镜像内仍带 nginx，由 supervisord 第四个 program 管理。监听从 `127.0.0.1:13001`
改成 `0.0.0.0:${BLUEPRINT_GATEWAY_PORT}` 才能被容器外访问——**这需要把 nginx 模板
的端口占位符化**（见 1.4）。

### 1.4 nginx 模板端口占位符化

**改造**（`deploy/nginx/blueprint-re.conf.template`）：

```
listen 127.0.0.1:13001;     →  listen __NGINX_LISTEN__;
proxy_pass http://127.0.0.1:13002;  →  proxy_pass http://127.0.0.1:__FRONTEND_PORT__;
proxy_pass http://127.0.0.1:18001;  →  proxy_pass http://127.0.0.1:__BACKEND_PORT__;
```

现有 `deploy_user_systemd.sh` / `deploy_release.sh` 渲染时填默认值
（`127.0.0.1:13001` 等），保持 Linux 现状不变；Docker 启动脚本填容器内值。

同步处理 `backend/app/main.py:59-67` 的 CORS 硬编码：把
`"http://127.0.0.1:13001"` / `"http://localhost:13001"` 从字面量改成基于
`settings.frontend_origin`（已经 env 可配），避免改端口后 CORS 失效。

### 1.5 Dockerfile + 启动脚本

新建 `deploy/docker/`：

```
deploy/docker/
  Dockerfile              # 多阶段构建
  entrypoint.sh           # 渲染 env、起 supervisord
  supervisord.conf.template
  nginx.container.conf    # 容器版 nginx 配置（端口走 env）
```

**Dockerfile 多阶段**（基于现有 release bundle 内容，最大化复用）：

```dockerfile
# Stage 1: runtime base（micromamba + conda env）
FROM mambaorg/micromamba:1.5-jammy AS runtime
# 建 conda env：python=3.13 nodejs>=22.19 nginx git bubblewrap（备用）
# 装 omicverse、R 环境到 envs/omicverse、envs/blueprint-re-r
# （复用 runtime/blueprint-re-python.yml / blueprint-re-r.yml）

# Stage 2: app
FROM runtime AS app
COPY wheels/ /tmp/wheels/
RUN /opt/conda/bin/pip install --no-index --find-links /tmp/wheels/ blueprint_re_backend
COPY frontend-standalone/ /app/frontend-standalone/
COPY manager-agent/ /app/manager-agent/
COPY deploy/docker/ /app/deploy/docker/

ENV BLUEPRINT_DATA_ROOT=/data \
    BLUEPRINT_EXECUTOR_SANDBOX_MODE=container \
    BLUEPRINT_CONTAINER_ISOLATED=true
VOLUME /data
EXPOSE 13001
ENTRYPOINT ["/app/deploy/docker/entrypoint.sh"]
```

**`entrypoint.sh` 职责**：

1. 首次启动：`/data` 不存在则初始化目录结构（`_system/` 等）。
2. 生成 `BLUEPRINT_INTERNAL_TOOL_TOKEN`（若未提供）。
3. 渲染 supervisord.conf + nginx.conf（端口走 env）。
4. exec `supervisord -c ...`。

### 1.6 镜像构建脚本

复用现有 `build_release_bundle.sh` 产出的 payload（wheels、frontend-standalone、
manager-agent、runtime/），新增 `scripts/build_docker_image.sh`：

```bash
# 1. 调 build_release_bundle.sh 产出 payload（或直接复用 dist/ 里已有的）
# 2. docker build -f deploy/docker/Dockerfile -t rhinedatalab:<version> .
# 3. docker tag 多架构（buildx）：linux/amd64（先做）；linux/arm64 待 R/omicverse arm64 轮子齐备
```

**多架构注意**：`manylinux_2_17_x86_64` wheel 在 arm64 容器装不上。Docker 产物先只发
`linux/amd64` 镜像；arm64 等 omicverse + R 的 aarch64 轮子补齐再开（这是上游依赖
问题，不是本方案能解决的）。

### 1.7 配置注入

用户侧两条路：

```bash
# A. 环境变量
docker run -e BLUEPRINT_DEEPSEEK_API_KEY=sk-xxx ...

# B. 挂 .env（仓库根 .env 格式，与现有一致）
docker run -v $PWD/.env:/app/.env:ro ...
```

`entrypoint.sh` 读 `/app/.env`（若挂了）合并进环境，再交给各进程。env 白名单逻辑
从 `deploy_release.sh` 抽出共享函数，容器里只设白名单内的 key。

### 1.8 Docker 产物验收清单

- [ ] `docker run` 单条命令起服务，`curl localhost:13001` 返回前端
- [ ] `/data` volume 持久化：重建容器后项目/运行历史还在
- [ ] `executor_sandbox_mode=container` 下，executor 子进程能正常跑（无 bwrap）
- [ ] `data_mount` 输入在 container 模式下能挂载、能执行（门控放行）
- [ ] 上传大文件（`/upload-api`）走 nginx 流式，不被缓冲截断
- [ ] 停容器 → 重启容器，四个进程都被 supervisord 拉起（等价现 systemd `Restart=always`）
- [ ] 现有 Linux systemd 部署路径回归通过（bwrap 模式未受影响）

---

## 产物二：macOS 原生应用

### 目标

双击 `RhineDataLab.app`，窗口打开，自动拉起本地服务，零额外安装。

### 2.1 整体架构

```
RhineDataLab.app（Electron）
  ├─ Contents/
  │   ├─ MacOS/RhineDataLab        # Electron 主进程（进程管家）
  │   ├─ Resources/
  │   │   ├─ app.asar              # Electron 渲染层 + 主进程 JS
  │   │   └─ runtime/              # 全捆绑运行时
  │   │       ├─ micromamba        # macOS universal2
  │   │       ├─ envs/omicverse    # macOS arm64+x86_64
  │   │       ├─ envs/blueprint-re-r
  │   │       ├─ node/             # Node 22 universal
  │   │       └─ backend/          # macOS wheel 装好的 venv
  │   │       └─ frontend-standalone/
  │   │       └─ manager-agent/
  │   └─ Info.plist
```

**首次启动流程**（Electron 主进程 `app.whenReady()`）：

1. 检查 `~/Library/Application Support/RhineDataLab/runtime/` 是否已展开。
2. 若无：从 `Resources/runtime/` 解压 micromamba + 建用户运行时目录（写用户家，
   因为 `.app` 包内是只读的）。
3. 选空闲端口（见 2.4），写到一个 `runtime.env`。
4. 起 manager-agent → backend → frontend（子进程，PID 记录）。
5. `mainWindow.loadURL("http://127.0.0.1:<gateway_port>")`。
6. 窗口关闭/退出：逆序 kill 子进程。

### 2.2 Electron 工程结构

新建 `desktop/` 顶层目录（与 `frontend/`、`backend/`、`manager-agent/` 平级）：

```
desktop/
  package.json            # electron + electron-builder 依赖
  src/
    main.js               # 主进程：生命周期 + 进程管家
    process-manager.js    # 拉起/监控/关闭后端三服务
    port-picker.js        # 选空闲端口
    first-run.js          # 运行时展开到 ~/Library/Application Support
  build/
    icon.icns
    Info.plist.template
  electron-builder.yml    # 打包配置（universal2）
```

`electron-builder.yml` 关键项：

```yaml
appId: com.rhinedatalab.app
productName: RhineDataLab
mac:
  target:
    - dmg
    - zip
  arch:
    - universal
  category: public.app-category.developer-tools
  extraResources:
    - from: ../dist/macos-runtime/
      to: runtime/
```

### 2.3 进程管家（复用产物一的编排思路）

`desktop/src/process-manager.js` 与 Docker 的 supervisord 职责对等，但用 Node
原生 `child_process.spawn` 实现（Electron 主进程本来就是 Node）：

```js
async function startAll(env) {
  const mgr = spawn(runtimeNode, ["src/server.js"], { cwd: managerDir, env });
  await waitForPort(env.MANAGER_PORT);        // healthz 轮询
  const be  = spawn(venvPython, ["-m","uvicorn","app.main:app",
                  "--host","127.0.0.1","--port",env.BACKEND_PORT], {cwd: backendDir, env});
  await waitForPort(env.BACKEND_PORT);
  const fe  = spawn(runtimeNode, ["server.js"], { cwd: frontendDir, env: {...env,
                  HOSTNAME:"127.0.0.1", PORT:env.FRONTEND_PORT}});
  await waitForPort(env.FRONTEND_PORT);
  return { mgr, be, fe };
}
```

**重启策略**：子进程意外退出（非用户主动关）时自动拉起，等价 Docker
`autorestart=true` / systemd `Restart=always`（呼应上一次 502 故障的修复方向）。

### 2.4 端口策略（可配置 + 自动选空闲）

默认 13001/13002/18001/18002，但 macOS 上这些端口可能被占。`port-picker.js`：

```js
function pickPort(preferred) {
  if (isFree(preferred)) return preferred;
  return findFreePort();  // 退而求其次，记录到 runtime.env
}
```

四个端口各自独立选。选好后写进 `runtime.env`，三个子进程都从这个 env 读——
因为端口已经全 env 化（共享重构 1.4 做的）。Electron 窗口 loadURL 用实际选中的
gateway 端口。

### 2.5 Seatbelt 沙箱（复用产物一的沙箱抽象）

`backend/app/workers/sandbox/seatbelt.py`：把 `SandboxPlan` 翻译成 `.sb` profile。

bwrap → Seatbelt 概念映射：

| bwrap | Seatbelt |
|-------|----------|
| `--ro-bind src dst` | `(allow file-read* (subpath "src"))` + `(deny file-write* (subpath "dst"))` |
| `--bind src dst`（读写） | `(allow file-read* file-write* (subpath "src"))` |
| `--tmpfs path` | `(deny file* (subpath "path"))` |
| `--clearenv` + `--setenv` | Seatbelt 不管 env，由 renderer 在命令前 `env -i KEY=VAL ...` |
| `--die-with-parent` | Seatbelt 无直接等价，靠 process group + Electron 退出时 kill |

调用方式：`sandbox-exec -p <pid> -f <profile.sb> -- <cmd>`。

`Settings.executor_sandbox_mode = "seatbelt"` 时启用。macOS 启动时 Electron 主进程
在 `runtime.env` 里设 `BLUEPRINT_EXECUTOR_SANDBOX_MODE=seatbelt`。

**风险提示**：Seatbelt profile 语法比 bwrap 啰嗦，且不同 macOS 版本策略 token 有差异。
macOS 产物的 seatbelt renderer 要做 smoke test（类比 `_ensure_bwrap_runtime`）。

### 2.6 macOS 运行时构建（universal2）

这是 macOS 路径工作量最大的一块。`scripts/build_macos_runtime.sh`：

```bash
# 1. 下载 macOS universal2 micromamba
# 2. 建 conda env（python=3.13 universal2）
# 3. pip install backend deps：优先 macOS arm64/x86_64 wheel，缺的从源码编译
#    （omicverse 纯 Python 可装；其重依赖 numpy/scipy/pandas 要 universal2 wheel）
# 4. 建 R env（bioconductor）
# 5. 下载 Node 22 universal 二进制
# 6. npm ci manager-agent + frontend standalone build（macOS 上 build 才能跑）
# 7. 打包到 dist/macos-runtime/
```

**关键风险**：omicverse 及其重计算依赖（scipy/sklearn 等）的 macOS universal2 wheel
未必齐备。需要先验证 `pip install omicverse` 在 macOS arm64 上能否纯 wheel 安装。
若不行，universal2 降级为「分别构建 arm64 和 x86_64 两个 .dmg，electron-builder
`arch: [arm64, x64]`」——这是 fallback，不影响整体架构。

### 2.7 `/home/` 脱敏正则修复

**改造**（`diagnostic_bundle_service.py:27` 等）：

```python
HOME_PATH_RE = r"/home/[^/\s]+"     # 现状：漏 /Users/
```

改为：

```python
HOME_PATH_RE = r"/(?:home|Users)/[^/\s]+"
```

涉及文件：`diagnostic_bundle_service.py`、`blueprint_review_worker.py`、
`card_library_service.py`。这其实是通用 bug 修复（Docker 产物也建议带上，因为容器里
`/data` 路径可能也含用户信息）。

### 2.8 macOS 状态目录

遵循 macOS 约定（`AGENTS.md` 的 `Path And Privacy Rules` 要求用 `Path.home()`）：

- 数据 `data_root` → `~/Library/Application Support/RhineDataLab/data`
  （`BLUEPRINT_DATA_ROOT` 覆盖，产物一 1.2 已做）
- 运行时展开 → `~/Library/Application Support/RhineDataLab/runtime/`
- 日志 → `~/Library/Logs/RhineDataLab/`

`project_registry.json` 里存的绝对路径：首次启动做一次 reconcile（现有
`project_service.py:75-87` 已有 legacy fallback），把旧路径迁移到新位置。

### 2.9 代码签名与公证（可选，发布前必做）

- Developer ID Application 证书签名 `.app`。
- `xcrun notarytool submit` 公证。
- Electron-builder 的 `afterSign` 钩子接 `@electron/notarize`。
- 未签名版本：用户首次打开会被 Gatekeeper 拦，需右键打开。

macOS 产物可以先出未签名版内部测试，签名公证作为发布前最后一道。

### 2.10 macOS 产物验收清单

- [ ] 双击 `.app`，窗口打开，无需任何额外安装
- [ ] 首次启动运行时展开到 `~/Library/Application Support/`，第二次启动秒开
- [ ] executor 在 `seatbelt` 模式下能跑（含 `data_mount` 输入）
- [ ] 关窗/退出，三个后台进程都干净退出
- [ ] 后台进程崩溃，Electron 自动拉起（呼应 502 教训）
- [ ] 端口被占时自动选空闲端口，UI 仍能打开
- [ ] universal2（或双架构）在 Intel + Apple Silicon Mac 上都能跑

---

## 共享重构清单（三产物都依赖）

按依赖顺序：

1. **沙箱抽象**（1.1）——抽 `SandboxRenderer`，bwrap 搬迁，新增 container renderer。
   macOS 的 seatbelt renderer 在此基础上加。
2. **`data_root` env 化**（1.2）——Docker 挂卷、macOS 换目录都靠它。
3. **端口/env 模板化**（1.4）——nginx 端口占位符、CORS 跟随 `frontend_origin`。
4. **进程编排层**（1.3 + 2.3）——Docker 用 supervisord、macOS 用 Electron 主进程，
   职责对等，配置同源；Linux 继续用 systemd（已是编排实现）。
5. **`/home/` 正则修复**（2.7）——通用 bug，顺手修。

---

## 工作量预估（粗估，供排期）

| 项 | 产物 | 量级 |
|----|------|------|
| 沙箱抽象 + bwrap 搬迁 + container renderer | 共享 | 大（核心重构，要回归测试） |
| `data_root` env 化 + 测试 | 共享 | 小 |
| nginx 端口模板化 + CORS 修复 | 共享 | 小 |
| supervisord 配置 + Dockerfile + entrypoint | Docker | 中 |
| Docker 镜像构建脚本 + 多阶段调通 | Docker | 中 |
| 镜像内置完整运行时（omicverse + R） | Docker | 中（依赖上游 wheel） |
| Docker 验收 + Linux 回归 | Docker | 中 |
| **Docker 产物小计** | | **~2-3 人周** |
| Electron 工程 + 进程管家 | macOS | 中 |
| macOS 运行时 universal2 构建 | macOS | 大（omicverse/macOS wheel 是最大风险） |
| Seatbelt renderer + smoke test | macOS | 大（profile 语法 + 兼容性） |
| 首次启动运行时展开 + 端口选择 | macOS | 中 |
| macOS 目录约定 + 脱敏修复 | macOS | 小 |
| 签名公证（发布前） | macOS | 中（需 Apple 开发者账号） |
| macOS 验收 | macOS | 中 |
| **macOS 产物小计** | | **~4-6 人周** |

**最大风险**：macOS universal2 运行时（omicverse + 重计算依赖的 arm64 wheel 齐备性）。
如果上游 wheel 不全，universal2 要降级双架构，或 macOS 上执行器功能受限。

**注意**：Docker 小计在前不代表 macOS 是“二等产物”。按 `docs/65` 的 IDE 定位，macOS
桌面是与 Linux/Docker 并列的一等产物。先做 Docker 纯粹因为它的工程不确定性低，可以
尽早验证共享重构（沙箱抽象、进程编排）是否成立，降低 macOS 路径的风险。

---

## 不在本次范围

- Docker 镜像的 arm64 支持（等上游 aarch64 wheel）
- Windows 原生应用（需求未提）
- macOS 上 opencode/claude_code/codex 执行器适配（先保证 pi）
- Kubernetes 部署 manifest（单镜像够用）
- 自动更新机制（electron-updater 可后续加）

---

## 建议的实施顺序

1. 先做沙箱抽象 + `data_root` env 化 + 端口模板化（共享重构，1-2 天）。
2. Docker 镜像跑通 + 验收（验证共享重构成立，闭环）。
3. 发布 Docker 镜像。
4. macOS：Electron 壳先能打开窗口（接现有 systemd/本地起的服务）。
5. macOS 运行时构建 + 全捆绑。
6. Seatbelt 沙箱。
7. 签名公证 + 发布 macOS App。

每一步都可独立验证、独立回滚，不存在"全做完才能用"的风险。

> 说明：`docs/66` 的执行模型与依赖链重设计（选区运行 + 失效传播）与本方案正交，
> 可与上述任何步骤并行实现，互不阻塞。
