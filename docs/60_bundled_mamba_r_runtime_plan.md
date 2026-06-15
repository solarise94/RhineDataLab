# 计划：端到端依赖解决方案升级 — 内置运行时 + 健壮解析器

> 目标：让大陆网络环境下的用户安装 Blueprint RE 后，**不需要自己装 conda、不需要装 R、不需要调镜像**，分析卡缺包时**自动探测、自动 fallback、自动安装、自动重试**，全程零终端操作。触发场景见 `rnaseq-test1` 诊断包（DESeq2 缺包反复失败 4 次，用户只能反复点"重新运行"）。
> 2026-06-15。

这是一个**整体依赖解决方案升级**，分两条互相配合的战线：
- **分发层（W1）**：内置 mamba + R 运行时 + 镜像，让用户开箱即有一个可用的、配好镜像的分析环境。
- **解析层（W2）**：让 resolver 在探测超时/solver 出错时正确让出 registry fallback（CRAN/Bioconductor/pip），并对 Bioconductor-only 包正确选族，使缺包能被后台 job 自动装上。

两层缺一不可：分发层让探测能成功，解析层让"探测失败但有 registry 回退"也能走通。DESeq2 场景只有两层都修才彻底闭环。

---

## 背景：现在卡在哪

诊断包 `rnaseq-test1_diagnostic_bundle_20260615T033116Z` 的完整失败链：

1. DESeq2 卡跑 4 次（6/9 三次 + 6/12 一次），每次 25–30 秒即失败，全是 `runtime_dependency_missing` / `DESeq2 is not installed`。
2. Manager **确实调用了** `resolve_runtime_dependencies`，但 resolver 在 conda 通道探测时**超时 60s**（大陆网络 + 无镜像），返回 `solver_error`。
3. resolver 把 `solver_error` 归为 `PACKAGE_STATUS_SOLVER_ERROR`，`_aggregate_status` 直接走 manual preparation 分支，**不触发** Bioconductor fallback。
4. Manager 只能给用户两个手动选项（手动装 / 换运行时），用户不会操作，只能反复点"重新运行"，永远跑不通。

根因是**两层叠加**：

- **分发层缺失**：用户机器没有可用的 conda/mamba（或装了没配镜像）、没有装了生信包的 R。每次卡缺包都要从零对抗网络。
- **解析层缺陷**：探测超时被当成"包不存在"而非"探测这一刻没成功"，堵死了 registry fallback 通道；且 DESeq2 这种 Bioconductor-only 包会被错误归到 CRAN 族。

---

## 现状审计（已经有什么、缺什么）

### 分发层

| 已有 | 位置 | 说明 |
|---|---|---|
| release bundle 有 `runtime/` 目录 | `scripts/build_release_bundle.sh:131, 289-325` | 只装系统级（python/nodejs/nginx/bwrap），**不含 mamba、不含 R、不含生信包** |
| `--offline-cache` 选项 | `build_release_bundle.sh:24, 304-325` | 预下载 conda 包，但 `environment.yml` 只有系统依赖 |
| 安装脚本探测 conda/R | `install_blueprint_re.sh:68-127` | **只探测不安装**，探测不到就留空 |
| deploy marker 机制 | `deploy_user_systemd.sh:8, 127-206` | SHA-256 marker 跳过未变步骤，可复用 |

| 缺失 |
|---|
| 没有内置 mamba 二进制（用户没 conda 就没戏） |
| 没有内置 R 运行时（完全依赖用户自备） |
| 没有镜像配置（大陆直连官方源经常超时） |
| 没有生信基础包（DESeq2 等从不预装） |
| installer 只探测不 provision（探测失败就放弃） |

### 解析层

| 已有 | 位置 | 说明 |
|---|---|---|
| resolver 按名识别 solver | `runtime_dependency_resolver_service.py:_batch_prefetch_conda` (line 595)、`_probe_conda` (line 1188) | repoquery 路径已认 mamba/micromamba |
| R fallback 族声明 | `runtime_dependency_resolver_service.py:122` `FALLBACK_FAMILIES_R=["cran","bioconductor"]` | 族知道，但选择逻辑有缺陷 |
| Bioconductor 安装命令 | `manager_blueprint_tools.py:1971-1976` `BiocManager::install(...)` | 已实现，**但写死 `cloud.r-project.org`，没镜像** |
| `allow_safe_registry_install` 策略 | `config.py:146` | 默认开启，允许安全 registry 安装 |
| 后台 job + dedupe + retry_hint | `runtime_dependency_job_service.py` + `runtime_dependency_state_service.py` | 完整 |

| 缺陷 |
|---|
| `find_conda_solver`（`config.py:46-57`）查找列表只有 `("mamba","conda")`，**不含 micromamba**——这是分发层的硬缺口 |
| `_resolve_package` 把超时归为 `PACKAGE_STATUS_SOLVER_ERROR`，`_aggregate_status` 里 solver_error 直接 manual，**不让出 fallback**（`runtime_dependency_resolver_service.py:833-846`） |
| `_single_safe_fallback_family` 对 R 包靠"都含 cran 就选 cran"收敛，DESeq2 只在 Bioconductor 上却会被错选 CRAN（`runtime_dependency_resolver_service.py:1405-1428`） |
| `_run_r_registry_install` 写死 `cloud.r-project.org`（`manager_blueprint_tools.py:1968,1973`），大陆环境下 CRAN/BiocManager 安装同样超时 |
| Bioconductor 包没有"已知 Bioconductor-only"识别，全靠探测，探测失败就全堵 |

---

## W1：分发层 — 内置 mamba + R 运行时 + 镜像

### W1 决策

#### 决策 1：用 micromamba，不用 miniconda/mamba

| 选项 | 体积 | 许可证 | 离线分发 | 结论 |
|---|---|---|---|---|
| **micromamba**（C++ 单文件） | ~15 MB | BSD-3 | 单二进制塞进 bundle | ✅ |
| miniforge3 + mamba | ~400 MB | MIT/BSL | 需安装脚本 | ❌ 太重 |
| miniconda3 | ~500 MB，anaconda 商业许可限制再分发 | 受限 | 不适合 bundle | ❌ 许可证风险 |

micromamba 单二进制、无依赖、专为离线/CI 设计，**独立发布在 `mamba-org/micromamba-releases`**（不是主仓 `mamba-org/mamba`；后者发的是完整 mamba Python 包）。下载 URL、版本约定、env 落地点都和 conda 不同，下面逐项说明。

**重要**：resolver 的 `_batch_prefetch_conda`（line 595）和 `_probe_conda`（line 1188）已按 solver 名识别 micromamba 并走 repoquery，但 `config.py:find_conda_solver`（line 46）的查找列表只有 `("mamba","conda")`，**不含 micromamba**——这是 W1-A4 必须修的（见下），不是"零适配"。

#### 决策 2：R + 生信核心包打包成 conda env，不裸装 R

在内置 micromamba 下创建 env `blueprint-re-r`，装 R + 核心生信包。原因：conda env 自带正确 ABI 的 R + gfortran + BLAS，避免用户机器缺系统库编译失败；resolver 已知如何探测 conda env 的 Rscript（`command_worker.py:_resolve_rscript_runtime`）；缺包时用同一个 micromamba 补装，路径一致。

基础包清单（覆盖 80% RNA-seq 流程，体积可控）：

```yaml
# deploy/runtime/blueprint-re-r.yml
name: blueprint-re-r
channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/bioconda
dependencies:
  - r-base =4.4
  - r-tidyverse
  - r-pheatmap
  - bioconductor-deseq2
  - bioconductor-edger
  - bioconductor-limma
  - bioconductor-clusterprofiler
  - bioconductor-org.hs.eg.db
  - bioconductor-org.mm.eg.db
```

#### 决策 3：默认清华镜像，可切换

镜像预设集中在 `deploy/runtime/mirror-presets/`（installer-local，可接受集中配置）。`BLUEPRINT_MIRROR_PRESET=tsinghua`（默认大陆）/`ustc`/`default`（海外/CI）。**preset 是全套镜像的总开关**：一个 preset 同时展开成 conda/CRAN/Bioconductor/PyPI 四套镜像配置，避免用户分别配。

| preset | conda（.mambarc） | CRAN | Bioconductor | PyPI |
|---|---|---|---|---|
| `tsinghua` | mirrors.tuna.tsinghua.edu.cn/anaconda/... | mirrors.tuna.tsinghua.edu.cn/CRAN | mirrors.tuna.tsinghua.edu.cn/bioconductor | mirrors.tuna.tsinghua.edu.cn/pypi/web/simple |
| `ustc` | mirrors.ustc.edu.cn/anaconda/... | mirrors.ustc.edu.cn/CRAN | — | pypi.mirrors.ustc.edu.cn/simple |
| `default` | repo.anaconda.com（官方） | cloud.r-project.org | bioconductor.org | pypi.org/simple |

install 脚本根据 preset 把各个 `*_mirror` 环境变量写入 backend.env；backend settings 默认值留空（空 = 官方源），由 install 注入实际值。这样海外用户不设 preset 就走官方源，不被默认值拖慢。

#### 决策 4：离线缓存 + 在线回退双模

bundle 带：(1) `runtime/micromamba`（必带，15MB）；(2) `runtime/pkgs/`（可选，`--with-r-cache`，~2GB 预下载）。在线机器实时建 env，离线机器从本地缓存建 env。发布标准包（~100MB）和 full 包（~2GB）两种。

### W1 实施（分阶段）

#### W1-A：内置 micromamba + 镜像（1-2 天）

本阶段有 **4 个必须配套的硬性改动**，任一缺失都会让 fresh machine 跑不通。

**W1-A1. 新增镜像预设（`.mambarc`，不是 `.condarc`）**

micromamba 读 `$MAMBA_ROOT_PREFIX/.mambarc`，且尊重 `$MAMBARC` 环境变量。`.condarc` 只在显式 `MAMBA_ROOT_PREFIX` 指向的目录下、且 micromamba 版本 ≥ 1.5 部分兼容。**保险做法：用 `.mambarc`，并通过 `$MAMBARC` 显式指向。**

```
deploy/runtime/mirror-presets/
├── tsinghua.mambarc
├── ustc.mambarc
├── default.mambarc
└── mirror_env.sh          # preset → 各 *_mirror 环境变量的展开表
```

`tsinghua.mambarc`：

```yaml
channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/bioconda
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
ssl_verify: true
```

`mirror_env.sh`（install 脚本 source 它来展开 preset）：

```bash
# tsinghua preset
tsinghua_conda_base_url="https://mirrors.tuna.tsinghua.edu.cn/anaconda"
tsinghua_cran_mirror="https://mirrors.tuna.tsinghua.edu.cn/CRAN"
tsinghua_bioconductor_mirror="https://mirrors.tuna.tsinghua.edu.cn/bioconductor"
tsinghua_pypi_mirror="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
# default preset
default_cran_mirror="https://cloud.r-project.org"
default_pypi_mirror="https://pypi.org/simple"
# bioconductor 官方无独立镜像域名，default 留空走 BiocManager 默认
```

**W1-A2. `build_release_bundle.sh`：下 micromamba 进 bundle（URL 已修正）**

micromamba 独立发布在 `mamba-org/micromamba-releases`，tag 形如 `2.8.0-0`（带 build 号后缀），asset 名 `micromamba-linux-64`（不带 `-0`）。在 "Gather runtime dependency metadata" 段（line 288 附近）新增：

```bash
# micromamba 独立发布仓库是 mamba-org/micromamba-releases（不是 mamba-org/mamba）。
# tag 带 build 后缀（2.8.0-0），asset 名不带。
MICROMAMBA_VERSION="2.8.0-0"
MICROMAMBA_URL="https://github.com/mamba-org/micromamba-releases/releases/download/${MICROMAMBA_VERSION}/micromamba-linux-64"
mkdir -p "${BUNDLE_ROOT}/runtime/bin"
echo "Downloading micromamba ${MICROMAMBA_VERSION}..."
# 构建机在 CI 上能访问 github
curl -fsSL "${MICROMAMBA_URL}" -o "${BUNDLE_ROOT}/runtime/bin/micromamba"
chmod +x "${BUNDLE_ROOT}/runtime/bin/micromamba"
# 冒烟：确认是能跑的二进制
"${BUNDLE_ROOT}/runtime/bin/micromamba" --version || die "micromamba binary broken"
cp -a "${REPO_ROOT}/deploy/runtime/mirror-presets" "${BUNDLE_ROOT}/runtime/mirror-presets"
```

在 `release.json` artifacts 登记 `runtime/micromamba`。**deploy marker 的输入 hash 要纳入 micromamba 版本**（见 W1-B3 / 风险表），否则 micromamba 升级不触发 env 重建。

**W1-A3. `install_blueprint_re.sh`：`provision_bundled_mamba()`（含 `MAMBA_ROOT_PREFIX`）**

micromamba 的 env 默认位置由 `$MAMBA_ROOT_PREFIX` 决定，**不是** `<base>/envs/`。如果只拷二进制不设 root prefix，后面 `micromamba create -n blueprint-re-r` 会落到 `~/micromamba/envs/`，而 resolver/command_worker 从 conda_base 反查 env 时（`derive_conda_base_from_runtime_path`，config.py:60）会回到 `~/micromamba`，与落地目录脱钩。**修法：把 base 本身当 root prefix，并落到稳定路径。**

新增函数，在 `detect_conda_base` 失败时兜底（line 209 附近）：

```bash
provision_bundled_mamba() {
  local bundled="${ROOT_DIR}/runtime/bin/micromamba"
  local target="${HOME}/.local/share/blueprint-re/mamba"   # 同时作为 MAMBA_ROOT_PREFIX
  [[ -x "${bundled}" ]] || return 1
  mkdir -p "${target}/bin" "${target}/envs"
  cp "${bundled}" "${target}/bin/micromamba"

  # micromamba 用 .mambarc（不是 .condarc）。显式写 root_prefix 到 .mambarc，
  # 并通过 MAMBARC 环境变量在子进程里强制指向它。
  local preset="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
  if [[ -f "${ROOT_DIR}/runtime/mirror-presets/${preset}.mambarc" ]]; then
    cp "${ROOT_DIR}/runtime/mirror-presets/${preset}.mambarc" "${target}/.mambarc"
  fi
  echo "root_prefix: ${target}" >> "${target}/.mambarc"

  # 把 root prefix / mambarc 落到 backend.env，让 backend 子进程（含 bwrap 内）能继承。
  # 这两个变量必须在 deploy 白名单里（见 W1-B4 / deploy_user_systemd.sh known_set）。
  export MAMBA_ROOT_PREFIX="${target}"
  export MAMBARC="${target}/.mambarc"
  printf '%s\n' "${target}"
}
```

修改探测链路兜底：

```bash
if [[ -z "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" ]]; then
  BLUEPRINT_EXECUTOR_CONDA_BASE="$(
    detect_conda_base 2>/dev/null \
    || provision_bundled_mamba 2>/dev/null \
    || true
  )"
fi
```

**关键**：`MAMBA_ROOT_PREFIX` 和 `MAMBARC` 必须透传给所有消费方——backend settings、bwrap sandbox（`--clearenv` 后要加回）、deploy systemd env。详见 W1-B4。

**命名分层（避免混淆）**：这里有两层名字，不要混用：
- **进程级变量**（micromamba 二进制约定，**不可改名**）：`MAMBA_ROOT_PREFIX`、`MAMBARC`。上面 `export MAMBA_ROOT_PREFIX=...` 是给 micromamba 子进程用的，保持原名。
- **backend 配置键**（我们控制，和 executor 命名空间一致）：`BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX`、`BLUEPRINT_EXECUTOR_MAMBARC`。这是 backend.env / `Settings` 字段名（`executor_mamba_root_prefix` / `executor_mambarc`），由 install 脚本从进程级变量展开写入。backend 读到 `Settings.executor_mamba_root_prefix` 后，在拉起 micromamba 子进程时再 `export MAMBA_ROOT_PREFIX=<值>`。

即：用户/install 侧的配置键加 `BLUEPRINT_EXECUTOR_` 前缀；真正喂给 micromamba 的进程变量用原名。下面凡写 `MAMBA_ROOT_PREFIX`/`MAMBARC` 均指进程级；凡写 `BLUEPRINT_EXECUTOR_MAMBA_*` 均指配置键。

**W1-A4. 探测逻辑认 micromamba（`find_conda_solver` 必须改）**

这是分发层的硬缺口。`config.py:find_conda_solver`（line 46）当前：

```python
for name in ("mamba", "conda"):   # ← 不含 micromamba
```

改为（micromamba 优先，因为更快、不污染 channel metadata）：

```python
def find_conda_solver(conda_base: Path) -> Path | None:
    """Search ``conda_base`` for a conda solver executable.

    Prefers ``micromamba`` (fastest, standalone), then ``mamba``, then ``conda``.
    Checks both ``bin/`` and ``condabin/``. Returns ``None`` when no solver found.
    """
    for name in ("micromamba", "mamba", "conda"):
        for subdir in ("bin", "condabin"):
            candidate = conda_base / subdir / name
            if candidate.exists():
                return candidate
    return None
```

`default_conda_base_candidates`（line 27）加内置路径：

```python
Path.home() / ".local/share/blueprint-re/mamba",   # ← 内置 mamba 落地处（= MAMBA_ROOT_PREFIX）
```

`install_blueprint_re.sh:detect_conda_base`（line 68）和 `deploy_user_systemd.sh:detect_conda_base`（line 368）同步加该候选，并把 `[[ -x "${candidate}/bin/conda" ]]` 扩展为也认 micromamba：

```bash
if [[ -x "${candidate}/bin/conda" || -x "${candidate}/bin/micromamba" ]]; then
```

**W1-A 验收**：fresh machine（无 conda）跑 installer 后，`BLUEPRINT_EXECUTOR_CONDA_BASE` 指向内置 mamba，`find_conda_solver` 返回 `bin/micromamba`，`MAMBA_ROOT_PREFIX` 已设，resolver 用 `.mambarc` 里的清华镜像探测不再 60s 超时。

#### W1-B：内置 R 环境（2-3 天）

**W1-B1. 新增 `deploy/runtime/blueprint-re-r.yml`**（见决策 2 清单）

**W1-B2. `build_release_bundle.sh`：`--with-r-cache`**

扩展 `--offline-cache`（line 304）或新增 `--with-r-cache`，用内置 micromamba 预下载 R env 包到 `runtime/pkgs/`：

```bash
if [[ "${BUILD_R_CACHE}" -eq 1 ]]; then
  mkdir -p "${BUNDLE_ROOT}/runtime/pkgs"
  "${BUNDLE_ROOT}/runtime/bin/micromamba" create -y \
    -p "${BUNDLE_ROOT}/runtime/.tmp-r-env" \
    -f "${REPO_ROOT}/deploy/runtime/blueprint-re-r.yml" \
    --download-only 2>/dev/null || true
  cp -a "${BUNDLE_ROOT}/runtime/.tmp-r-env/pkgs/." "${BUNDLE_ROOT}/runtime/pkgs/" 2>/dev/null || true
  rm -rf "${BUNDLE_ROOT}/runtime/.tmp-r-env"
fi
```

**W1-B3. `install_blueprint_re.sh`：`provision_bundled_r_runtime()`（完整版）**

新增，`detect_default_r_runtime` 失败时调用（line 215 附近）。完整逻辑含命令、离线分支、幂等判定、失败回滚、marker 输入：

```bash
provision_bundled_r_runtime() {
  local mamba_base="$1"          # = MAMBA_ROOT_PREFIX，如 ~/.local/share/blueprint-re/mamba
  local r_env="blueprint-re-r"
  local env_spec="${ROOT_DIR}/runtime/blueprint-re-r.yml"
  local local_pkgs="${ROOT_DIR}/runtime/pkgs"
  local micromamba_bin="${mamba_base}/bin/micromamba"
  [[ -x "${micromamba_bin}" && -f "${env_spec}" ]] || return 1

  local env_dir="${mamba_base}/envs/${r_env}"
  # 幂等：env 已存在直接跳过
  if [[ -x "${env_dir}/bin/Rscript" ]]; then
    printf '%s\n' "${r_env}"
    return 0
  fi

  echo "Provisioning bundled R runtime (${r_env})... this may take 5-20 min."
  export MAMBA_ROOT_PREFIX="${mamba_base}"
  export MAMBARC="${mamba_base}/.mambarc"

  # 在线 vs 离线：有本地缓存且非空走 --offline
  local create_cmd=("${micromamba_bin}" create -y -n "${r_env}" -f "${env_spec}")
  if [[ -d "${local_pkgs}" ]] && [[ -n "$(ls -A "${local_pkgs}" 2>/dev/null)" ]]; then
    create_cmd+=(--channel "${local_pkgs}" --offline)
  fi

  # 失败回滚：避免半建 env 污染下次 rerun
  if ! "${create_cmd[@]}"; then
    echo "R runtime provisioning failed; cleaning partial env." >&2
    "${micromamba_bin}" env remove -n "${r_env}" -y 2>/dev/null || true
    return 1
  fi
  printf '%s\n' "${r_env}"
}
```

修改 R 探测链路（line 215 附近）：

```bash
if [[ -z "${BLUEPRINT_DEFAULT_R_RUNTIME:-}" ]]; then
  BLUEPRINT_DEFAULT_R_RUNTIME="$(
    detect_default_r_runtime "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" 2>/dev/null \
    || provision_bundled_r_runtime "${BLUEPRINT_EXECUTOR_CONDA_BASE:-}" 2>/dev/null \
    || true
  )"
fi
```

**deploy marker**（见 W1-B4）：输入 hash = `blueprint-re-r.yml` 的 SHA-256 **+ micromamba 版本**（micromamba 升级可能影响 env metadata 格式）。**只在成功后写 marker**——失败不写，下次 rerun 会重建（配合上面的失败回滚）。

**W1-B4. bwrap 沙箱 + env 透传（关键子任务）**

AGENTS.md 明确 `BLUEPRINT_EXECUTOR_SANDBOX_MODE=bwrap` 是必选，且 bwrap 用 `--clearenv`。两个问题：

1. **bwrap sandbox_plan.json 需要 bind-mount bundled env 路径**（`~/.local/share/blueprint-re/mamba/envs/blueprint-re-r`）。当前 `command_worker._wrap_with_bwrap` 按 `_resolve_rscript_runtime` 解析到的 env 做 bind，但前提是 MAMBA_ROOT_PREFIX 已传入子进程，否则 micromamba 在子进程里找不到 env。
2. **`--clearenv` 后必须显式加回 `MAMBA_ROOT_PREFIX` / `MAMBARC`**，否则 bwrap 内的 R 子进程 `R_LIBS_USER` / `.libPaths()` 会丢 conda 的 site-library，`library(DESeq2)` 失败。

改动点：

- `command_worker._wrap_with_bwrap`（line 292 附近）的 `env_keys` 集合（line 426）加 `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX`、`BLUEPRINT_EXECUTOR_MAMBARC`（配置键）。`build_launch_spec` 在拼 bwrap `--setenv` 时，要把这两个键的**值**以进程级变量名 `MAMBA_ROOT_PREFIX`/`MAMBARC` 注入子进程（即 `--setenv MAMBA_ROOT_PREFIX <Settings.executor_mamba_root_prefix 的值>`）。
- `sandbox_plan.json` 的 `writable_binds`/`readonly_binds` 把 `${MAMBA_ROOT_PREFIX}` 整目录纳入（至少 `envs/blueprint-re-r`，建议整个 root prefix 只读 bind，体积小）。
- `deploy_user_systemd.sh` 的 backend.env 写入白名单（`known_set`，line ~329）加 `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX`、`BLUEPRINT_EXECUTOR_MAMBARC`、`BLUEPRINT_CRAN_MIRROR`、`BLUEPRINT_BIOCONDUCTOR_MIRROR`、`BLUEPRINT_PYPI_MIRROR`。（用 `BLUEPRINT_EXECUTOR_` 前缀，和 `BLUEPRINT_EXECUTOR_CONDA_BASE` / `BLUEPRINT_DEFAULT_R_RUNTIME` 同域一致；`*_MIRROR` 是 registry 安装用，跨 executor 共享，不加 EXECUTOR_ 前缀。）
- backend `Settings`（config.py）加 `executor_mamba_root_prefix`/`executor_mambarc` 字段，install 脚本从进程级 `MAMBA_ROOT_PREFIX`/`MAMBARC`/preset 展开写入这两个配置键。

**W1-B 验收**：fresh machine 装完，`BLUEPRINT_DEFAULT_R_RUNTIME=blueprint-re-r`，bwrap 子进程内进程级 `MAMBA_ROOT_PREFIX` 可见（由 backend 从 `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX` 配置键注入），`micromamba run -n blueprint-re-r Rscript -e 'library(DESeq2)'` 成功，DESeq2 卡直接通过。

#### W1-C：Python 分析运行时（可选，1 天）

对称 provision `blueprint-re-py`（scanpy/anndata/omicverse）。**注意 PyPI 在大陆同样不稳**，W1-C 一并引入 `pypi_mirror` setting（默认空=官方源，preset 注入清华）。建议 W1-A 就预留 `pypi_mirror` settings 字段（默认空），W1-C 实际启用。

#### W1-D：文档与诊断（0.5 天）

- `docs/for_agent_install.md` 补充 "Bundled mamba + R runtime" 章节，说明标准包/full 包、`BLUEPRINT_MIRROR_PRESET`、`MAMBA_ROOT_PREFIX` 透传、如何扩展 `blueprint-re-r.yml`。
- `project_service._python_runtimes`（line 1060）/ `_r_runtimes`（line 1104）给每个 runtime dict 加 `"source": "bundled"|"system"|"conda"`，`diagnostic_bundle_service._system_info` 透传，远程排障一眼看出用户是否在用内置运行时。

---

## W2：解析层 — 健壮的 fallback 与族选择

W1 让探测能成功，但网络抖动/包不在 conda 通道/solver 临时出错时，解析层必须正确让出 registry fallback。当前三个缺陷要修。

### W2-1：超时/solver_error 降级为 fallback_required（关键）

**问题**：`_resolve_package`（`runtime_dependency_resolver_service.py:833`）把超时归为 `PACKAGE_STATUS_SOLVER_ERROR`，`_aggregate_status`（line 937）里 solver_error 直接走 manual，不让出 fallback。

**修法**：区分"确定性不存在"和"探测这一刻失败"。当包有 fallback 族可用（R 包永远有 cran/bioconductor，Python 包永远有 pip）时，探测超时/solver_error 应降级为 `PACKAGE_STATUS_FALLBACK_REQUIRED`，让 `allow_safe_registry_install` 策略能接住。

`_resolve_package` 改动（line 833 附近）：

```python
if probe_result.status == "solver_error":
    # 探测失败不代表包不存在。当该生态有 registry fallback 族可用且策略允许时，
    # 降级为 fallback_required 而非 solver_error，让 fallback 策略接住。
    fallback = _fallback_families_for(ecosystem)
    if fallback and getattr(self, "_active_policy", "report_only") == "allow_safe_registry_install":
        return ResolverPackageEntry(
            name=pkg,
            normalized_name=normalized,
            classification=classification,
            conda_candidates=candidates,
            fallback_available=fallback,
            status=PACKAGE_STATUS_FALLBACK_REQUIRED,
            reason=f"conda_probe_failed:{probe_result.error_code or 'unknown'}",
            message=(
                f"Conda probe failed for {pkg!r} ({probe_result.error_detail or 'unknown'}); "
                "registry fallback available under the active policy."
            ),
        ), None
    # 无 fallback 族或策略不允许：保持 solver_error（原行为）
    return ResolverPackageEntry(... status=PACKAGE_STATUS_SOLVER_ERROR ...), None
```

（直接用现有 `_active_policy` 字符串比较，不引入新函数。）

**边界**：确定性"包不在 conda 通道"（`probe_result.status == "not_found"`，即 `_is_packages_not_found` 命中）**不降级**——那个路径已经是 `FALLBACK_REQUIRED`（line 848），是对的。只改 solver_error/超时这一支。

**影响面**：仅当 conda 探测超时/报错 **且** 包有 fallback 族 **且** 策略允许时改变行为；其余情况保持原样。低风险。

### W2-2：Bioconductor-only 包正确选族

**问题**：`_single_safe_fallback_family`（line 1405）对 R 包靠"所有包都含 cran 就选 cran"收敛。DESeq2 只在 Bioconductor，却因为 `fallback_available=["cran","bioconductor"]` 含 cran 而被错选 CRAN，导致 `install.packages("DESeq2")` 失败。

**修法**：新增 Bioconductor-only 包识别。维护一个已知 Bioconductor 包集合，命中则强制 `bioconductor` 族。

新增模块级常量（已去重，包名均已核对存在于 Bioconductor 主仓）：

```python
# 已知仅存在于 Bioconductor（不在 CRAN）的常见包。
# 命中时 fallback 族强制选 bioconductor，避免 install.packages 失败。
# 包名核对来源：bioconductor.org/packages（R 里 library() 小写均可）。
BIOCONDUCTOR_ONLY_PACKAGES: frozenset[str] = frozenset({
    "deseq2", "edger", "limma", "clusterprofiler", "complexheatmap",
    "sva", "genefilter", "genomicfeatures", "rtracklayer",
    "annotationdbi", "biomart", "goseq", "pathview", "reactomepa",
    "gsva", "scran", "scater", "soupx",
})
```

新增选族函数：

```python
def _select_r_fallback_family(packages: Iterable[ResolverPackageEntry]) -> str | None:
    """为 R 包请求选择单一安全 fallback 族。

    优先级：
    1. 任何包命中 BIOCONDUCTOR_ONLY_PACKAGES → 整个请求走 bioconductor
       （BiocManager::install 能装 CRAN 包，混入 CRAN 包也安全）
    2. 所有包只有一个族 → 那个族
    3. 所有包都含 cran → cran（原收敛逻辑，保留向后兼容）
    """
    entries = list(packages)
    if not entries:
        return None
    lowered = {e.name.lower() for e in entries}
    if lowered & BIOCONDUCTOR_ONLY_PACKAGES:
        return "bioconductor"
    # 原逻辑（_single_safe_fallback_family 之后的部分）
    ...（保留原收敛）
```

让 `_populate_installable_actions`（line 862）和 `collect_fallback_actions`（line 1580）对 R 生态调用 `_select_r_fallback_family` 替代原 `_single_safe_fallback_family`。

**边界**：`BIOCONDUCTOR_ONLY_PACKAGES` 只含确定性 Bioconductor 包；未收录的包若探测 `not_found` 仍走原 cran 收敛，`BiocManager::install` 对 CRAN 包也能成功，不会卡死。

### W2-3：CRAN/Bioconductor 安装走镜像

**问题**：`_run_r_registry_install`（`manager_blueprint_tools.py:1968, 1973`）写死 `https://cloud.r-project.org`，大陆环境下 CRAN/BiocManager 安装同样超时，W2-1/W2-2 让出了 fallback 通道但安装本身还是失败。

**修法**：新增 settings 字段（`config.py`），**默认值留空（空 = 官方源）**，由 install 脚本按 preset 注入实际值（见决策 3）。这样海外用户不设 preset 就走官方源，不被默认值拖慢：

```python
# R registry mirrors。默认空 = 官方源；install 脚本按 BLUEPRINT_MIRROR_PRESET 注入。
cran_mirror: str = ""
bioconductor_mirror: str = ""
pypi_mirror: str = ""   # W1-C 用，W1-A 预留
```

`_run_r_registry_install`（`manager_blueprint_tools.py:1966`）改用 settings：

```python
settings = self.project_service.settings
cran = getattr(settings, "cran_mirror", "") or "https://cloud.r-project.org"
bioc = getattr(settings, "bioconductor_mirror", "") or ""
if installer_type == "cran":
    expression = (
        f'options(repos=c(CRAN={_json.dumps(cran)})); '
        f'install.packages({package_vector}, dependencies=TRUE)'
    )
else:  # bioconductor
    # 顺序很重要：必须先 install.packages("BiocManager")（确保包已加载），
    # 再 options(BioC_mirror=...)（此时 BiocManager 命名空间已可用），
    # 最后让 BiocManager::install 内部自己调用 repositories() 读取镜像。
    # 注意：不能在 BiocManager 加载前调用裸 repositories()——会因找不到函数而报错。
    bioc_opt = f'options(BioC_mirror={_json.dumps(bioc)}); ' if bioc else ''
    expression = (
        f'options(repos=c(CRAN={_json.dumps(cran)})); '
        'if (!requireNamespace("BiocManager", quietly=TRUE)) install.packages("BiocManager"); '
        f'{bioc_opt}'
        f"BiocManager::install({package_vector}, ask=FALSE, update=FALSE)"
    )
```

**注意（BiocManager 镜像行为）**：顺序必须是 `install.packages("BiocManager")` → `options(BioC_mirror=...)` → `BiocManager::install(...)`。`BiocManager` 倾向于在 `install` 内部调用 `repositories()` 读取 `BioC_mirror` option；裸 `repositories()` 不能在 BiocManager 加载前调用（找不到函数）。`BioC_mirror` 的尊重程度因版本而异，落地时需实测；若 option 不生效，回退为直接用 Bioconductor 仓库 URL 经 `install.packages`（绕过 BiocManager）。测试用例 7 要断言这个顺序。

**边界**：`cran_mirror`/`bioconductor_mirror` 留空时回退 `cloud.r-project.org`/BiocManager 默认，即原行为。deploy 白名单（W1-B4）同步加这两个 key。海外端到端验收（验证策略 §7）要明确验证"默认值空不破坏海外环境"。

### W2：测试

新增 `backend/tests/test_runtime_resolver_fallback.py`：

1. conda 探测返回 `probe_timeout` → 包状态降级为 `fallback_required`（策略 allow）→ 请求级 `fully_installable`（单族）。
2. conda 探测返回 `solver_error` 同上。
3. conda 探测 `not_found`（确定性不存在）保持原 `fallback_required`（不回归）。
4. DESeq2 单包请求 → 族选 `bioconductor`（不是 cran）。
5. DESeq2 + ggplot2 混合请求 → 族选 `bioconductor`（BiocManager 能装 ggplot2）。
6. 纯 ggplot2 请求 → 族选 `cran`（不误伤）。
7. mock `_run_r_registry_install`，断言 CRAN/Bioconductor 镜像被正确注入 R 表达式，且顺序为 `install.packages("BiocManager")` → `options(BioC_mirror=...)` → `BiocManager::install(...)`（裸 `repositories()` 不得出现在 BiocManager 加载前）。

---

## 涉及文件清单（整体）

| 文件 | 改动 | 战线 | 阶段 |
|---|---|---|---|
| `deploy/runtime/mirror-presets/*.mambarc` + `mirror_env.sh` | 新增 | W1 | A |
| `deploy/runtime/blueprint-re-r.yml` | 新增 | W1 | B |
| `scripts/build_release_bundle.sh` | 改：下 micromamba（URL 修正）、`--with-r-cache` | W1 | A+B |
| `scripts/install_blueprint_re.sh` | 改：`provision_bundled_mamba`（含 MAMBA_ROOT_PREFIX/.mambarc）、`provision_bundled_r_runtime`、兜底、preset 展开 | W1 | A+B |
| `scripts/deploy_user_systemd.sh` | 改：`detect_conda_base` 认 micromamba、R env marker、env 白名单加 `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX`/`BLUEPRINT_EXECUTOR_MAMBARC`/`*_MIRROR` | W1+W2 | A+B |
| `backend/app/core/config.py` | 改：`find_conda_solver` 加 micromamba、`default_conda_base_candidates` 加内置路径、新增 `cran_mirror`/`bioconductor_mirror`/`pypi_mirror`、`executor_mamba_root_prefix`/`executor_mambarc` settings | W1+W2 | A+W2-3 |
| `backend/app/workers/command_worker.py` | 改：bwrap `env_keys` 加 `BLUEPRINT_EXECUTOR_MAMBA_ROOT_PREFIX`/`BLUEPRINT_EXECUTOR_MAMBARC`、bind bundled env | W1 | B |
| `backend/app/services/runtime_dependency_resolver_service.py` | 改：solver_error→fallback 降级、`_select_r_fallback_family`、`BIOCONDUCTOR_ONLY_PACKAGES` | W2 | W2-1/2 |
| `backend/app/services/manager_blueprint_tools.py` | 改：`_run_r_registry_install` 用 settings 镜像 | W2 | W2-3 |
| `backend/app/services/project_service.py` | 改：runtime dict 加 `source` | W1 | D |
| `backend/app/services/diagnostic_bundle_service.py` | 改：透传 `source` | W1 | D |
| `backend/tests/test_runtime_resolver_fallback.py` | 新增 | W2 | — |
| `backend/tests/test_config_runtime_candidates.py` | 新增：测 `default_conda_base_candidates`/`find_conda_solver`（mock 文件存在性） | W1 | A |
| `scripts/test_provisioning.sh`（bash 验收脚本） | 新增：temp HOME 跑 `provision_bundled_*` | W1 | A+B |
| `docs/for_agent_install.md` | 改：补充说明 | W1 | D |

> **测试分工**：Python unittest 测 `config.py` 的纯函数（候选路径、solver 查找，mock 文件存在性）；bash 的 `provision_bundled_*` 用独立 bash 验收脚本（仿 AGENTS.md behavior-based 测试，但不在 Python unittest 里跑），避免 Python 测 bash 函数的错配。

---

## 验证策略

### 单元/行为测试（CI）

1. `test_runtime_resolver_fallback.py`：W2 的 7 个用例（见上）。
2. `test_config_runtime_candidates.py`：temp HOME + mock 文件存在性——`default_conda_base_candidates` 在只有内置 mamba 路径时返回内置路径；`find_conda_solver` 对 `bin/micromamba` 返回 micromamba 且优先于 conda。

### bash 验收测试（独立脚本，非 unittest）

`scripts/test_provisioning.sh`：临时 HOME 跑 `provision_bundled_mamba` / `provision_bundled_r_runtime`（mock micromamba 二进制和 yml），断言 `.mambarc`/`MAMBA_ROOT_PREFIX`/env 目录正确生成、失败回滚生效、幂等跳过。

### 端到端验收（手动，发布前）

干净 Ubuntu 22.04 容器（无 conda、无 R）：

1. 跑 `bash scripts/install_blueprint_re.sh`。
2. 验证 `~/.local/share/blueprint-re/mamba/bin/micromamba` 存在，`.mambarc` 是清华镜像，`MAMBA_ROOT_PREFIX` 指向该目录。
3. 验证 `BLUEPRINT_DEFAULT_R_RUNTIME=blueprint-re-r`，`MAMBA_ROOT_PREFIX` 在 bwrap 子进程内可见，`micromamba run -n blueprint-re-r Rscript -e 'library(DESeq2)'` 成功。
4. 跑 `rnaseq-test1` 的 DESeq2 卡，应直接通过。
5. **W2 回归**：构造一个不在 `blueprint-re-r.yml` 里的包（如 `bioconductor-sva`），手动触发 resolver，验证：(a) 探测走镜像不超时；(b) 命中 Bioconductor 族；(c) 后台 job 用镜像成功安装；(d) 卡自动重试通过。
6. **超时回退**：临时断 conda 探测（或指向不可达地址），验证 resolver 降级为 fallback，Bioconductor 安装仍能成功（因 Bioconductor 镜像独立于 conda 探测）。
7. **海外模式**：`BLUEPRINT_MIRROR_PRESET=default` 重复，验证 `cran_mirror`/`bioconductor_mirror`/`pypi_mirror` 留空时走官方源，端到端 work——即默认值不破坏海外环境。

### 网络退化测试

1. 断网跑 full 包（带 `runtime/pkgs/`），纯离线 `micromamba create --offline` 建成 env。
2. 标准包断网，installer 给清晰错误（"需要网络或 full 包"），不静默失败。

---

## 风险与权衡

| 风险 | 应对 |
|---|---|
| **bundle 体积膨胀**（full ~2GB） | 分标准/full 两种发布；micromamba 仅 15MB；W1-A 单独发布就解决探测超时 |
| **镜像源不稳定**（清华偶尔同步滞后） | 预设多镜像（tsinghua/ustc），`BLUEPRINT_MIRROR_PRESET` 可切；保留 `default` |
| **micromamba 平台覆盖** | 锁版本 + linux-x86_64（和 release bundle `platform: linux, arch: x86_64` 一致） |
| **micromamba 升级影响 env metadata** | deploy marker 输入 hash 纳入 micromamba 版本（W1-B3），升级自动触发重建 |
| **micromamba 升级时半建 env** | 失败回滚 `micromamba env remove`；marker 只在成功后写 |
| **R 包 ABI 与系统库** | conda env 自带 gfortran/BLAS，不依赖宿主系统库（决策 2 的理由） |
| **生信包清单不全** | 基础包覆盖 80%，缺的靠 W1-A 的 mamba + 镜像现场装 + W2 的 fallback 自动装 |
| **W2-1 误降级**（真不存在但探测报 solver_error） | 降级后走 fallback 安装，安装失败仍报 `dependency_install_failed`（retry_hint=inspect_stderr），不静默成功；比当前"堵死在 manual"更好 |
| **W2-2 名单过时**（新 Bioconductor 包未收录） | 名单只兜底常见包；未收录的包若探测 not_found 仍走原 cran 收敛，BiocManager::install 对 cran 包也能成功，不会卡死 |
| **BiocManager 不尊重 BioC_mirror option** | W2-3 表达式顺序 `install.packages("BiocManager")` → `options(BioC_mirror=...)` → `BiocManager::install(...)`；测试用例 7 断言顺序（裸 `repositories()` 不得在 BiocManager 加载前）；若 option 不生效回退 `install.packages` + Bioconductor 仓库 URL |
| **bwrap `--clearenv` 丢 MAMBA_ROOT_PREFIX** | W1-B4 把 `MAMBA_ROOT_PREFIX`/`MAMBARC` 加入 env_keys 白名单 + bind env 目录 |
| **海外用户被默认镜像拖慢** | settings 默认值留空（=官方源），preset 才注入清华；验证策略 §7 明确回归 |

---

## 与现有架构的契合

- **不碰** worker 核心 / bwrap 沙箱模型 / sandbox plan 结构 / 前端 / manager-agent 核心逻辑。只动分发层、runtime 探测层、resolver 的 fallback 判定、R 安装镜像、bwrap 的 env 白名单。
- **复用** resolver 的 mamba/micromamba repoquery 预取、deploy marker、后台 job/dedupe/retry_hint 全套。
- **遵守** AGENTS.md：路径不硬编码（镜像集中到预设文件/settings）、不引入硬限制、bwrap 沙箱不变（只加白名单）、secrets 不进 git、改 Pydantic 后重生成 schemas。
- **不破坏** 用户自备 conda/R 路径——内置 runtime 是探测链路**最后兜底**；W2 的改动在"无 fallback 族或策略不允许"时完全保持原行为。

---

## 上线节奏

两条战线可并行，但建议：

1. **W1-A + W2 一起发**（核心闭环，~1 周）。W1-A 内置 micromamba + 镜像 + `find_conda_solver` 修正 + `MAMBA_ROOT_PREFIX` 透传，让探测能成功；W2 让探测失败也能走 fallback + Bioconductor 镜像安装。发布 patch release。这两者配合就能让 DESeq2 这类场景**自动走通**（哪怕包不在基础清单）。
2. **W1-B 紧随**（2-3 天）。内置 R env 让核心流程真正零现场安装。发布 minor release，分标准/full 包。
3. **W1-C、W1-D 视反馈**。

### DESeq2 场景走通后的预期路径

修复后 `rnaseq-test1` 再跑 DESeq2 卡（两种情况都通）：

**情况 1：包在基础清单**（W1-B 后）
1. installer 已 provision `blueprint-re-r`，内含 `bioconductor-deseq2`。
2. 卡的 `r_env = "blueprint-re-r"`，`MAMBA_ROOT_PREFIX` 在 bwrap 内可见，`library(DESeq2)` 直接成功。

**情况 2：包不在基础清单**（W1-A + W2 后，如 `bioconductor-sva`）
1. executor 探测到缺包，报 `runtime_dependency_missing`。
2. Manager 调 `resolve_runtime_dependencies`，resolver 用内置 micromamba + `.mambarc` 清华镜像探测（秒级）。
3. 命中 conda 通道 → `fully_installable` → 后台 job `micromamba install` → 卡自动重试通过。
4. 或 conda 探测超时/未命中 → W2-1 降级为 `fallback_required` → W2-2 选 `bioconductor` 族 → 后台 job `BiocManager::install`（走 W2-3 清华 Bioconductor 镜像）→ 卡自动重试通过。

用户全程零干预、零终端操作。这正是"开箱即跑"的完整含义。
