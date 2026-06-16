# Blueprint RE Agent Install Guide

本文件给接手仓库的 agent 使用。目标不是解释产品，而是让 agent 能稳定把程序装起来。

## Agent Task Protocol

安装 agent 必须按四个阶段推进，不要只启动服务就结束。

1. 环境探查与依赖解决
   - 检查 OS、shell、`systemd --user`、本地端口、用户 namespace / `bubblewrap` 能力。
   - 使用 release installer 时，不要要求宿主机预装 Python、Node.js、nginx、git 或 `bubblewrap`；这些应由安装器在用户目录 runtime env 中提供或引导。
   - 使用源码 fallback 时，再检查 `git`/`gh`、Python `3.13+`、Node.js `22.19.0+`、`npm`、Conda/Mamba、R/Rscript、`bubblewrap`。
   - **检查 `pi` CLI 是否存在。这是安装主路径的唯一必需执行器。**
   - 可选检查 `opencode`、`claude`、`codex` CLI 是否存在并记录状态；不要因它们缺失而阻塞安装。
   - 判断 systemd 服务是否能看到 `pi`；如果不能，改用绝对路径 `*_COMMAND_JSON`。
   - 如果是 WSL 或路径包含空格，必须使用 JSON argv。
   - 缺依赖时先解决；不能因为 `bwrap` 失败而切到无沙箱。

2. 项目下载与部署
   - 私有仓库先处理 GitHub 登录。
   - 有 release artifact 时，优先运行自解压单文件安装器；源码 checkout 只作为开发者 fallback。
   - release installer 生成 `~/.config/blueprint-re/*.env`，并把 release、runtime env、data、logs 放在 `~/.local/share/blueprint-re/` 下。
   - 源码 fallback 才 clone/pull 项目、生成仓库根 `.env`，再运行源码安装/部署脚本。
   - 启动 `blueprint-re-nginx.service`、`blueprint-re-backend.service`、`blueprint-re-frontend.service`、`blueprint-re-manager-agent.service`。
   - 检查后端 `healthz` 和 nginx HTTP 响应。缺少 provider key 时，`manager-agent` 可以处于 degraded/failing 状态，不应单独判定为安装失败。

3. 配置与本机烟雾测试
   - 询问用户选择：输入项目 API key/base URL 做 `project_api` smoke，或使用本机已登录的 CLI 做 `cli_native` smoke。
   - 默认推荐 `pi` 的 `project_api`；`opencode` 支持 `project_api` 和 `cli_native`；`claude_code`、`codex` 只做 `cli_native`。
   - 根据本机结果调整 `*_COMMAND_JSON`、API base URL、executor profile 和 CLI 绝对路径。
   - 确认 Claude Code 使用 `"-p", "@{executor_prompt_path}"`。
   - 有 API key 或用户明确要求真实 smoke 时，再创建最小临时项目/卡片跑真实执行器；否则只做服务、profile、CLI 可用性 smoke。

4. 部署完成后的总结
   - 输出前端/后端 URL。
   - 输出四个 systemd service 状态。
   - 输出默认执行器和可用 profiles。
   - 列出已自动修复的本机兼容性配置。
   - 列出仍需用户手动处理的 API key、CLI 登录、系统依赖或权限问题。

## Preferred Path

产品/用户安装统一使用版本化自解压安装包作为入口。同一个 `.sh` 包既能首次安装，也能升级已有安装：

```bash
sh rhinedatalab-<version>-linux-x86_64.sh
```

已有安装时，上述命令会自动进入升级流程，不需要额外参数。如果希望 CI/运维脚本明确“必须是升级、不允许误装成首次安装”，可显式传入 `--upgrade`：

```bash
sh rhinedatalab-<version>-linux-x86_64.sh --upgrade
```

`--upgrade` 在没有已有安装时会直接报错，避免脚本误以为完成了升级。

也可以通过 release 固定入口 `install.sh` 自动下载并校验对应版本的安装器：

```bash
curl -fsSL \
  https://github.com/solarise94/RhineDataLab/releases/latest/download/install.sh | \
  bash
```

指定版本：

```bash
VERSION=0.4.2
curl -fsSL \
  "https://github.com/solarise94/RhineDataLab/releases/download/v${VERSION}/install.sh" | \
  bash
```

`install.sh` 下载完成后会执行同版本的自解压安装器，行为与直接运行 `.sh` 包一致。

release installer 的职责：

- 不需要 `sudo` 或 `apt`
- 安装到 `~/.local/share/blueprint-re/`、`~/.config/blueprint-re/` 和 `~/.config/systemd/user/`
- 在 `~/.local/share/blueprint-re/env` 中准备 Python、Node.js、nginx、bubblewrap、git
- 在 `~/.local/share/blueprint-re/mamba` 中准备 standalone `micromamba`，并可选创建 `blueprint-re-r` conda 环境
- 安装 backend wheel 和 vendored Python dependency wheels
- 使用预构建 Next.js standalone frontend
- 使用已打包的 manager-agent production `node_modules`
- 生成并启动 user-mode systemd services

源码 checkout / 开发者 fallback 才使用：

```bash
bash scripts/install_blueprint_re.sh
```

如果你需要手工改参数，再使用：

```bash
bash scripts/install_blueprint_re.sh --interactive
```

源码 fallback 脚本负责：

- 检查并安装基础系统依赖（当前支持 apt 主机）
- 检查 `bubblewrap` 是否真的可用
- 显式校验 backend Python `3.13+` 与 Node.js `22.19.0+`
- 探测默认 Conda 目录
- 探测默认 Python runtime
- 探测默认 R runtime
- 生成 `.env`
- 调用 `scripts/deploy_user_systemd.sh`
- 安装并重启四套 `systemd --user` 服务

## Expected Host Assumptions

release installer 默认假设：

- Linux
- 用户可用 `systemd --user`
- 本地 shell 可运行 `bash`
- 有基础工具：`tar`、`sha256sum`、`mktemp`、`sed`、`grep`
- 如果 artifact 没有完整离线缓存，需要可用网络和 `curl` 或 `wget`
- 宿主内核/安全策略允许 user namespace 和 `bubblewrap`

源码 fallback 默认假设：

- Linux
- 用户可用 `systemd --user`
- 有 `apt-get`，或依赖已预装
- 后端安装 Python `3.13+`
- Node.js `22.19.0+`
- 允许安装：
  - `bubblewrap`
  - `python3`
  - `python3-venv`
  - `python3-pip`
  - `nodejs`
  - `npm`
- `git`
- `gh`
- `systemd`

如果主机不是 apt 系，源码 fallback 不要强行猜测包管理器；应明确告诉用户手动安装依赖后再继续。release installer 不应通过宿主包管理器修复 Python、Node.js、nginx、git 或 `bubblewrap`。

注意：apt 自动安装只负责基础包存在，不保证自动把 Python 升到 `3.13+`、或把 Node 升到 `22.19.0+`。版本不满足时，agent 应先解决语言运行时版本，再继续部署。

## Private Repo Bootstrap

如果仓库还是私有的，agent 应优先处理：

1. 检查 `gh` 是否已安装。
2. 如果没有，先安装 `gh`。
3. 执行 `gh auth login` 并引导用户完成授权。
4. 使用 `gh repo clone <owner>/<repo>` 或等价私有仓库 URL 完成下载。
5. 进入仓库后再运行安装脚本。

## API Key Policy

Release/user-mode install must not treat provider credentials as deploy gates.

- `BLUEPRINT_DEEPSEEK_API_KEY` — Runtime credential for provider-backed manager/pi execution. It is optional at install time.
- `TAVILY_API_KEY` — Optional manager-agent capability. It can be left empty during install and configured later.

If the user has not provided a key, continue the install and report that provider-backed features remain unavailable until credentials are configured. A no-key install is successful when backend `/healthz` and nginx are healthy; `manager-agent` may be degraded until runtime credentials are supplied.

## Config Ownership

部署后运行中的 systemd 服务，以 `~/.config/blueprint-re/*.env` 为唯一运行时真源。

职责边界：

- **仓库根 `.env`**：安装输入 / 部署种子。`scripts/install_blueprint_re.sh` 生成它，`scripts/deploy_user_systemd.sh` 消费它来创建 systemd env 文件。
- **`~/.config/blueprint-re/backend.env`**：backend 服务真正读取的运行时环境。
- **`~/.config/blueprint-re/manager-agent.env`**：manager agent 服务真正读取的运行时环境。
- **`~/.config/blueprint-re/frontend.env`**：frontend 服务真正读取的运行时环境。

**重要**：release installer 不依赖仓库根 `.env` 作为运行时真源。源码 fallback 中，修改仓库根 `.env` 后，必须重新执行 `bash scripts/deploy_user_systemd.sh`，或至少手动重新生成 `~/.config/blueprint-re/*.env` 并重启 systemd 服务。直接改根 `.env` 不会让运行中服务生效。

## Executor Selection And Auth

默认执行器应保持 `pi`。它是当前最佳兼容路径，支持项目 API 注入，安装脚本和 UI 都按它作为默认值处理。

其他执行器可安装，但要标注为部分兼容：

| Worker | 推荐程度 | 登录/项目 API | Tool policy 原生注入 | MCP 原生注入 | Skill 原生注入 | 配置要求 |
| --- | --- | --- | --- | --- | --- | --- |
| `pi` | 最佳兼容 | `project_api` 和 `cli_native` 均支持；默认推荐 project API 注入 | 部分支持，主要由 Blueprint prompt/bwrap 约束 | 不支持 | 支持，转换为 `pi --skill <path>` | 配好 DeepSeek/Pi 项目 API，或先在宿主机完成 Pi 登录 |
| `opencode` | 部分兼容 | `cli_native` 和 `project_api` 均支持 | 部分支持，写入 run-scoped capability config | 部分支持，写入 OpenCode config 并暴露 `OPENCODE_MCP_CONFIG` | 部分支持，写入 OpenCode config/env 的 skill paths | 原生登录或 OpenAI-compatible/provider-native 项目 API |
| `claude_code` | 部分兼容 | 仅 `cli_native` | 支持安全子集，映射为 `--permission-mode`、`--allowedTools`/`--disallowedTools` | 支持，映射为 `--mcp-config <path>` | 非原生，仅通过 Blueprint env/prompt 暴露 paths | 本机 Claude Code 已登录 |
| `codex` | 部分兼容 | 仅 `cli_native` | 不支持原生注入；仅 Blueprint prompt/bwrap 约束 | 不支持 | 不支持 | 本机 Codex CLI 已登录 |

`cli_native` 表示 wrapper 不注入项目 API key，不写 auth-bearing config，只让 CLI 使用系统已有登录态或本机配置。bwrap 会把原生目录作为只读路径暴露给执行器；如果 CLI 需要刷新 token，应先在宿主机手动登录，不能在 run 沙箱内刷新。

`project_api` 表示 wrapper 使用项目配置注入 API key/base URL/model。当前只应给 `pi` 和 `opencode` 使用；不要给 `claude_code` 或 `codex` 注入项目 API。

Agent 安装时不要把 `executor_profile` 当作 profile id 使用。`executor_profile` 是稳定 worker 名，例如 `pi_worker`、`opencode_worker`；真实选择的 profile id 应写入 `executor_profile_id`。

## Executor Command Templates

新安装必须优先写 `*_COMMAND_JSON`，不要优先写旧的 shell 字符串模板。

JSON argv 模板规则：

- 每个 JSON 数组元素就是一个 argv。
- 占位符只在单个 argv 元素内替换，不经过 shell 二次切分。
- 这能正确处理 WSL 和 Linux 下带空格的路径，例如 `/mnt/c/Users/xu/Documents/New project/...`。
- `{repo_root}` 指仓库根目录，适合引用仓库内脚本。
- `{executor_prompt_path}` 指当前 run 的 executor prompt。

推荐默认值（install/deploy 主路径自动配置前三项，`codex` 仅作为手工扩展示例）：

```env
BLUEPRINT_PI_COMMAND_JSON=["bash","{repo_root}/scripts/blueprint_pi_launch.sh","{executor_prompt_path}"]
BLUEPRINT_OPENCODE_COMMAND_JSON=["opencode","run","--file","{executor_prompt_path}","--format","json","--dangerously-skip-permissions","Read {executor_prompt_path} and complete the Blueprint executor contract exactly."]
BLUEPRINT_CLAUDE_CODE_COMMAND_JSON=["claude","-p","@{executor_prompt_path}","--output-format","stream-json","--verbose"]
# BLUEPRINT_CODEX_COMMAND_JSON — current install/deploy defaults do NOT auto-write this.
# If the user needs codex, add it manually based on host setup:
# BLUEPRINT_CODEX_COMMAND_JSON=["codex","exec","{executor_prompt_path}"]
```

旧变量 `BLUEPRINT_PI_COMMAND`、`BLUEPRINT_OPENCODE_COMMAND`、`BLUEPRINT_CLAUDE_CODE_COMMAND`、`BLUEPRINT_CODEX_COMMAND` 仍可作为兼容 fallback，但如果路径里有空格，agent 应改成 JSON argv，而不是尝试手写复杂 shell quoting。

WSL 特别注意：

- 不要把仓库路径中的空格当成用户错误；长期方案是 JSON argv。
- 不要让 `conda run /mnt/c/Users/.../New project/...` 通过 shell 字符串拼接。
- 不要因为 bwrap 只读挂载原生目录而改成无沙箱执行。

## Bundled Mamba + R Runtime

Release bundles (built with `scripts/build_release_bundle.sh`) now ship a standalone
`micromamba` binary in `runtime/bin/micromamba` and a set of mirror presets in
`runtime/mirror-presets/`. On a fresh machine without an existing conda/mamba/R setup,
the installer can provision a fully local runtime:

- `BLUEPRINT_MIRROR_PRESET` controls the mirror set. Defaults to `tsinghua` for mainland
  China; use `ustc` or `default` (official sources) as needed.
- `BLUEPRINT_INSTALL_R_RUNTIME` controls whether the bundled R env is built.
  `1` (default) installs the slim runtime covering DESeq2/edgeR/limma + tidyverse
  + plotting. `0` skips R entirely; micromamba is still installed.
- `BLUEPRINT_INSTALL_R_EXTRAS` controls whether enrichment packages are appended.
  `0` (default) keeps the slim runtime. `1` adds clusterProfiler + org.Hs/Mm.eg.db
  on top of the existing `blueprint-re-r` env using `micromamba install -n`,
  not a rebuild.
- `provision_bundled_mamba()` copies the bundled `micromamba` to
  `~/.local/share/blueprint-re/mamba`, writes a `.mambarc` with the selected preset,
  and exports `MAMBA_ROOT_PREFIX` / `MAMBARC`.
- `provision_bundled_r_runtime()` creates/updates the slim `blueprint-re-r` conda
  environment from `deploy/runtime/blueprint-re-r.yml`.
- `provision_bundled_r_extras()` appends `deploy/runtime/blueprint-re-r-extras.yml`
  to the same env when `BLUEPRINT_INSTALL_R_EXTRAS=1`.
- With `--with-r-cache`, the slim runtime packages are pre-downloaded into
  `runtime/pkgs/` for fully offline installs. Extras are packaged but not
  pre-downloaded because Bioconductor annotation DB tarballs cannot be reliably
  fetched in `--download-only` mode.

Backend settings:

- `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX` / `BLUEPRINT_EXECUTOR_MAMBARC` are written to
  `backend.env` and passed through bwrap as the process-level `MAMBA_ROOT_PREFIX` /
  `MAMBARC` variables so micromamba can locate envs inside the sandbox.
- `BLUEPRINT_CRAN_MIRROR`, `BLUEPRINT_BIOCONDUCTOR_MIRROR`, `BLUEPRINT_PYPI_MIRROR` are
  populated from the selected preset. Empty values mean "use the official source" so
  overseas deployments are not slowed down by hard-coded defaults.

Diagnostics include a `"source"` field on each runtime entry
(`"bundled" | "conda" | "system"`) so support bundles immediately show whether the
user is running the bundled runtime.

## Runtime Detection Rules

安装脚本按以下顺序探测 Conda base：

1. `BLUEPRINT_EXECUTOR_CONDA_BASE`
2. `CONDA_PREFIX`
3. `~/miniconda3`
4. `~/miniforge3`
5. `~/anaconda3`
6. `/opt/conda`

后端 bootstrap Python 必须满足 `>=3.13`。如果宿主机默认 `python3` 更低版本，agent 应先安装或切换到 Python 3.13，再运行部署。

默认 Python runtime 候选顺序：

1. `omicverse`
2. `analysis`
3. `base`

默认 R runtime 候选顺序：

1. `bioconductor`
2. `r-bio`
3. `base`
4. 如果系统里有 `Rscript`，则回落为 `__system__`

Node.js 也必须满足 `>=22.19.0`，因为 `frontend` 和 `manager-agent` 都按这个上限基线部署。

## After Install Verification

安装完成后，agent 应至少验证：

```bash
systemctl --user status blueprint-re-nginx.service
systemctl --user status blueprint-re-backend.service
systemctl --user status blueprint-re-frontend.service
systemctl --user status blueprint-re-manager-agent.service
```

以及：

```bash
curl -fsS http://127.0.0.1:18001/healthz
curl -I http://127.0.0.1:13001
```

安装成功基线是 backend `/healthz` 正常且 nginx gateway 可访问。没有 provider credentials 时，`blueprint-re-manager-agent.service` 可以 degraded/failing；agent 应记录原因并提示后续配置 key，而不是回滚或判定安装失败。

## Local Compatibility Smoke And Self-Repair

安装 agent 不能只验证服务启动。必须在当前宿主机做本机兼容性烟测，并根据结果自行调整配置。

### 1. Host command discovery

检查当前用户 shell 里能否找到执行器 CLI：

```bash
command -v pi || true
# Optional inventory — do NOT block install if missing
command -v opencode || true
command -v claude || true
command -v codex || true
```

如果 shell 能找到但 systemd 服务找不到，优先把 `*_COMMAND_JSON` 改成绝对路径，不要依赖交互 shell 的 `PATH`。

### 2. JSON argv and path-space check

必须确认安装生成的是 `*_COMMAND_JSON`，而不是只依赖旧的 `*_COMMAND` 字符串模板。

检查位置：

```bash
grep -E 'BLUEPRINT_.*COMMAND_JSON' .env ~/.config/blueprint-re/backend.env 2>/dev/null || true
```

如果仓库路径或 workspace 路径包含空格，必须使用 JSON argv。不要尝试用 shell quoting 修 `conda run /mnt/c/Users/.../New project/...` 这类问题。

Claude Code 特别注意：

```env
BLUEPRINT_CLAUDE_CODE_COMMAND_JSON=["/absolute/path/to/claude","-p","@{executor_prompt_path}","--output-format","stream-json","--verbose"]
```

`-p` 后面必须是 `@{executor_prompt_path}`，否则 Claude Code 会把路径字符串当成 prompt 内容。

### 3. bwrap smoke

部署脚本会做 bwrap smoke test。安装 agent 不允许因为 bwrap 失败而改成无沙箱执行。

如果失败，应报告宿主机 namespace/setuid/bubblewrap 配置问题，并停止执行器部署修复；不要把 `BLUEPRINT_EXECUTOR_SANDBOX_MODE` 静默改成 `none`。

### 4. Native login checks

对 `cli_native` profile，只能使用宿主机已有登录态；wrapper 不注入项目 API key。

安装 agent 应检查但不要修改这些目录内容。`pi` 登录态通常通过 `PI_CODING_AGENT_DIR` 或 `~/.pi/agent` 管理：

```bash
test -d "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}" && echo "Pi login dir present"
test -d "${XDG_CONFIG_HOME:-$HOME/.config}/opencode" && echo "OpenCode config dir present"
test -d "$HOME/.claude" && echo "Claude Code login dir present"
# Codex is optional; only check if the user explicitly asked for it
test -d "$HOME/.codex" && echo "Codex login dir present"
```

如果目录缺失或 CLI 要求重新登录，告诉用户在宿主机执行对应 CLI 登录。不要在 bwrap run 内尝试刷新登录。

### 5. Service-level smoke

修改 `.env` 或 `~/.config/blueprint-re/*.env` 后重启服务：

```bash
systemctl --user restart blueprint-re-nginx.service
systemctl --user restart blueprint-re-backend.service
systemctl --user restart blueprint-re-manager-agent.service
systemctl --user restart blueprint-re-frontend.service
```

然后重新检查：

```bash
systemctl --user status blueprint-re-nginx.service --no-pager
systemctl --user status blueprint-re-backend.service --no-pager
systemctl --user status blueprint-re-frontend.service --no-pager
systemctl --user status blueprint-re-manager-agent.service --no-pager
curl -fsS http://127.0.0.1:18001/healthz
curl -I http://127.0.0.1:13001
curl -fsS http://127.0.0.1:18001/api/executor-profiles
```

如果没有 provider credentials，`manager-agent` 未 active 只应作为 degraded runtime capability 记录。不要因为这一点重跑安装或把安装判为失败。

### 6. Executor smoke policy

如果用户没有提供项目 API key，不要强行跑会消耗外部模型调用的真实执行器 smoke。此时只验证：

- 服务启动。
- `/api/executor-profiles` 返回 profile 列表。
- `pi` 是默认最佳兼容 profile。
- backend 和 nginx 健康；`manager-agent` 如因缺少 provider credentials degraded，要在报告里说明。
- `opencode`、`claude_code`、`codex` 的 CLI 可用性和登录态状态被如实记录。

如果用户已配置 API key 或明确要求真实 smoke，创建一个临时项目/卡片做最小 run，检查 run 能产生 `agent_trace.json`、`manifest.json` 或清晰的失败原因。失败时优先修本机兼容性配置：

- CLI 绝对路径。
- `*_COMMAND_JSON`。
- API base URL。
- 原生登录态。
- bwrap 只读挂载可见性。

最后输出安装报告，必须包含：

- 前端/后端 URL。
- 四个 systemd service 状态。
- 当前默认执行器和可用 profiles。
- 已修复的本机兼容性配置。
- 仍需用户手动处理的 API key、CLI 登录或系统依赖。

## Source / Developer Fallback

`scripts/deploy_user_systemd.sh` 保留为源码/开发部署入口，不再作为普通用户升级路径推荐。只有在从源码树直接部署、或需要手动调整 `.env` 后再部署时才使用：

1. 准备仓库根 `.env`
2. 运行：

```bash
bash scripts/deploy_user_systemd.sh
```

普通用户应优先使用 release 自解压安装包 `rhinedatalab-<version>-linux-x86_64.sh` 完成首次安装和升级。

## Important Constraints

- 不要因为 `bwrap` 失败而静默降级到无沙箱执行。
- 不要把 API key 写进文档、日志或提交。
- 不要假设用户已经准备好 R/Bioconductor；如果没有，先装主程序，再让用户后续补 runtime。
- provider credentials 是运行时能力要求，不是 release/user-mode 安装 gate。缺 key 时安装应继续，manager/provider 功能可保持 degraded。
- 默认优先无交互安装；只有在用户明确要求时才切到 `--interactive`。
