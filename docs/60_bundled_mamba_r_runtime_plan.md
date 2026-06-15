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

1. DESeq2 卡跑 4 次（6/9 三次 + 6/12 一次），每次 25–30 秒秒失败，全是 `runtime_dependency_missing` / `DESeq2 is not installed`。
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
| resolver 优先 mamba，支持 micromamba | `config.py:find_conda_solver` + `_resolve_conda_solver` | 依赖宿主机已有 solver |
| R fallback 族声明 | `runtime_dependency_resolver_service.py:122` `FALLBACK_FAMILIES_R=["cran","bioconductor"]` | 族知道，但选择逻辑有缺陷 |
| Bioconductor 安装命令 | `manager_blueprint_tools.py:1971-1976` `BiocManager::install(...)` | 已实现，**但写死 `cloud.r-project.org`，没镜像** |
| `allow_safe_registry_install` 策略 | `config.py:146` | 默认开启，允许安全 registry 安装 |
| 后台 job + dedupe + retry_hint | `runtime_dependency_job_service.py` + `runtime_dependency_state_service.py` | 完整 |

| 缺陷 |
|---|
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

micromamba 单二进制、无依赖、专为离线/CI 设计。`find_conda_solver`（`config.py:52`）和 resolver 的 mamba 批量 repoquery 预取（`_batch_prefetch_conda` line 595）已原生支持，零适配。

#### 决策 2：R + 生信核心包打包成 conda env，不裸装 R

在内置 mamba 下创建 env `blueprint-re-r`，装 R + 核心生信包。原因：conda env 自带正确 ABI 的 R + gfortran + BLAS，避免用户机器缺系统库编译失败；resolver 已知如何探测 conda env 的 Rscript（`command_worker.py:_resolve_rscript_runtime`）；缺包时用同一个 mamba 补装，路径一致。

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

镜像预设集中在 `deploy/runtime/mirror-presets/`（installer-local，可接受集中配置）。`BLUEPRINT_MIRROR_PRESET=tsinghua`（默认大陆）/`ustc`/`default`（海外/CI）。

#### 决策 4：离线缓存 + 在线回退双模

bundle 带：(1) `runtime/micromamba`（必带，15MB）；(2) `runtime/pkgs/`（可选，`--with-r-cache`，~2GB 预下载）。在线机器实时建 env，离线机器从本地缓存建 env。发布标准包（~100MB）和 full 包（~2GB）两种。

### W1 实施（分阶段）

#### W1-A：内置 micromamba + 镜像（1-2 天）

**W1-A1. 新增镜像预设**

```
deploy/runtime/mirror-presets/
├── tsinghua.condarc
├── ustc.condarc
└── default.condarc
```

`tsinghua.condarc`：

```yaml
channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/bioconda
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
ssl_verify: true
```

**W1-A2. `build_release_bundle.sh`：下 micromamba 进 bundle**

在 "Gather runtime dependency metadata" 段（line 288 附近）新增：

```bash
MICROMAMBA_VERSION="1.5.10"
MICROMAMBA_URL="https://github.com/mamba-org/mamba/releases/download/${MICROMAMBA_VERSION}/micromamba-linux-64"
mkdir -p "${BUNDLE_ROOT}/runtime/bin"
echo "Downloading micromamba ${MICROMAMBA_VERSION}..."
curl -fsSL "${MICROMAMBA_URL}" -o "${BUNDLE_ROOT}/runtime/bin/micromamba"
chmod +x "${BUNDLE_ROOT}/runtime/bin/micromamba"
cp -a "${REPO_ROOT}/deploy/runtime/mirror-presets" "${BUNDLE_ROOT}/runtime/mirror-presets"
```

并在 `release.json` artifacts 登记 `runtime/micromamba`。

**W1-A3. `install_blueprint_re.sh`：`provision_bundled_mamba()`**

新增函数，落地到 `~/.local/share/blueprint-re/mamba/`，在 `detect_conda_base` 失败时兜底（line 209 附近）：

```bash
provision_bundled_mamba() {
  local bundled="${ROOT_DIR}/runtime/bin/micromamba"
  local target="${HOME}/.local/share/blueprint-re/mamba"
  [[ -x "${bundled}" ]] || return 1
  mkdir -p "${target}/bin"
  cp "${bundled}" "${target}/bin/micromamba"
  local preset="${BLUEPRINT_MIRROR_PRESET:-tsinghua}"
  [[ -f "${ROOT_DIR}/runtime/mirror-presets/${preset}.condarc" ]] \
    && cp "${ROOT_DIR}/runtime/mirror-presets/${preset}.condarc" "${target}/.condarc"
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

**W1-A4. 探测逻辑认 micromamba**

`config.py:default_conda_base_candidates`（line 27）加内置路径：

```python
Path.home() / ".local/share/blueprint-re/mamba",   # ← 内置 mamba 落地处
```

`install_blueprint_re.sh:detect_conda_base`（line 68）和 `deploy_user_systemd.sh:detect_conda_base`（line 368）同步加该候选，并把 `[[ -x "${candidate}/bin/conda" ]]` 扩展为也认 micromamba：

```bash
if [[ -x "${candidate}/bin/conda" || -x "${candidate}/bin/micromamba" ]]; then
```

**W1-A 验收**：fresh machine（无 conda）跑 installer 后，`BLUEPRINT_EXECUTOR_CONDA_BASE` 指向内置 mamba，`find_conda_solver` 返回 micromamba，resolver 用清华镜像探测不再 60s 超时。

#### W1-B：内置 R 环境（2-3 天）

**W1-B1. 新增 `deploy/runtime/blueprint-re-r.yml`**（见决策 2 清单）

**W1-B2. `build_release_bundle.sh`：`--with-r-cache`**

扩展 `--offline-cache`（line 304）或新增 `--with-r-cache`，用内置 micromamba 预下载 R env 包到 `runtime/pkgs/`。

**W1-B3. `install_blueprint_re.sh`：`provision_bundled_r_runtime()`**

新增，`detect_default_r_runtime` 失败时调用（line 215 附近）。在线用镜像建 env，有本地缓存走 `--offline`。幂等：env 已存在直接跳过（deploy marker 保护，marker 输入 = yml 的 SHA-256）。

**W1-B 验收**：fresh machine 装完，`BLUEPRINT_DEFAULT_R_RUNTIME=blueprint-re-r`，`micromamba run -n blueprint-re-r Rscript -e 'library(DESeq2)'` 成功，DESeq2 卡直接通过。

#### W1-C：Python 分析运行时（可选，1 天）

对称 provision `blueprint-re-py`（scanpy/anndata/omicverse）。Python 缺包率低（pip 直连 pypi 相对稳），建议 W1-B 验证后再做。

#### W1-D：文档与诊断（0.5 天）

- `docs/for_agent_install.md` 补充 "Bundled mamba + R runtime" 章节。
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
    # 探测失败不代表包不存在。当该生态有 registry fallback 族可用时，
    # 降级为 fallback_required 而非 solver_error，让 fallback 策略接住。
    fallback = _fallback_families_for(ecosystem)
    if fallback and fallback_policy_allows(getattr(self, "_active_policy", "report_only")):
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

**边界**：确定性"包不在 conda 通道"（`probe_result.status == "not_found"`，即 `_is_packages_not_found` 命中）**不降级**——那个路径已经是 `FALLBACK_REQUIRED`（line 848），是对的。只改 solver_error/超时这一支。

**影响面**：仅当 conda 探测超时/报错 **且** 包有 fallback 族 **且** 策略允许时改变行为；其余情况保持原样。低风险。

### W2-2：Bioconductor-only 包正确选族

**问题**：`_single_safe_fallback_family`（line 1405）对 R 包靠"所有包都含 cran 就选 cran"收敛。DESeq2 只在 Bioconductor，却因为 `fallback_available=["cran","bioconductor"]` 含 cran 而被错选 CRAN，导致 `install.packages("DESeq2")` 失败。

**修法**：新增 Bioconductor-only 包识别。维护一个已知 Bioconductor 包集合（DESeq2/edgeR/limma/clusterProfiler/ComplexHeatmap 等，这些是 Bioconductor core/release 包），命中则强制 `bioconductor` 族。

新增模块级常量（`runtime_dependency_resolver_service.py`）：

```python
# 已知仅存在于 Bioconductor（不在 CRAN）的常见包。
# 命中时 fallback 族强制选 bioconductor，避免 install.packages 失败。
BIOCONDUCTOR_ONLY_PACKAGES: frozenset[str] = frozenset({
    "deseq2", "edger", "limma", "clusterprofiler", "complexheatmap",
    "deseq2", "sva", "genefilter", "genomicfeatures", "rtracklayer",
    "annotationdbi", "biomart", "goseq", "pathview", "reactomepa",
    "gsva", "mseadatasets", "scran", "scater", "soupx",
})
```

新增选族函数：

```python
def _select_r_fallback_family(packages: Iterable[ResolverPackageEntry]) -> str | None:
    """为 R 包请求选择单一安全 fallback 族。

    优先级：
    1. 任何包命中 BIOCONDUCTOR_ONLY_PACKAGES → 整个请求走 bioconductor
       （Bioconductor 包混在 CRAN 请求里时，bioconductor installer 也能装 CRAN 包）
    2. 所有包只有一个族 → 那个族
    3. 所有包都含 cran → cran（原收敛逻辑，保留向后兼容）
    """
    entries = list(packages)
    if not entries:
        return None
    lowered = {e.name.lower() for e in entries}
    if lowered & BIOCONDUCTOR_ONLY_PACKAGES:
        return "bioconductor"
    # 原逻辑
    ...（保留 _single_safe_fallback_family 之后的收敛）
```

让 `_populate_installable_actions`（line 862）和 `collect_fallback_actions`（line 1580）都调用 `_select_r_fallback_family` 替代原 `_single_safe_fallback_family`（R 生态）。

**边界**：BIOCONDUCTOR_ONLY_PACKAGES 只包含确定性 Bioconductor 包，不含模棱两可的；`BiocManager::install` 本身能装 CRAN 包，所以"混入 CRAN 包"也安全。

### W2-3：CRAN/Bioconductor 安装走镜像

**问题**：`_run_r_registry_install`（`manager_blueprint_tools.py:1968, 1973`）写死 `https://cloud.r-project.org`，大陆环境下 CRAN/BiocManager 安装同样超时，W2-1/W2-2 让出了 fallback 通道但安装本身还是失败。

**修法**：从运行时配置读 CRAN/Bioconductor 镜像，默认清华。新增 settings 字段（`config.py`）：

```python
# R registry mirrors (中国大陆默认清华)
cran_mirror: str = "https://mirrors.tuna.tsinghua.edu.cn/CRAN"
bioconductor_mirror: str = "https://mirrors.tuna.tsinghua.edu.cn/bioconductor"
```

`_run_r_registry_install`（`manager_blueprint_tools.py:1966`）改用 settings：

```python
settings = self.project_service.settings
cran = getattr(settings, "cran_mirror", "https://cloud.r-project.org")
bioc = getattr(settings, "bioconductor_mirror", "")
if installer_type == "cran":
    expression = f'options(repos=c(CRAN={_json.dumps(cran)})); install.packages({package_vector}, dependencies=TRUE)'
else:  # bioconductor
    bioc_opts = f'options(BioC_mirror={_json.dumps(bioc)})' if bioc else ''
    expression = (
        f'options(repos=c(CRAN={_json.dumps(cran)})); {bioc_opts} '
        'if (!requireNamespace("BiocManager", quietly=TRUE)) install.packages("BiocManager"); '
        f"BiocManager::install({package_vector}, ask=FALSE, update=FALSE)"
    )
```

**注意**：`BiocManager::install` 的 BioC_mirror 需在 `BiocManager` 加载前设置，且新版 BiocManager 较少尊重 `BioC_mirror` option（它倾向于走 `BiocManager::repositories()`）。更稳的做法是在 R 表达式里显式 `options(BioC_mirror=...)` 后 `BiocManager::repositories()` 会读取。需要测试验证；若不行则改为 `BiocManager::options` 或直接用 `install.packages` + Bioconductor 仓库 URL。

**边界**：海外用户设 `cran_mirror=https://cloud.r-project.org`、`bioconductor_mirror` 留空即回退原行为。deploy 白名单（`deploy_user_systemd.sh` 的 `known_set`）要同步加这两个 key。

### W2：测试

新增 `backend/tests/test_runtime_resolver_fallback.py`：

1. conda 探测返回 `probe_timeout` → 包状态降级为 `fallback_required`（策略 allow）→ 请求级 `fully_installable`（单族）。
2. conda 探测返回 `solver_error` 同上。
3. conda 探测 `not_found`（确定性不存在）保持原 `fallback_required`（不回归）。
4. DESeq2 单包请求 → 族选 `bioconductor`（不是 cran）。
5. DESeq2 + ggplot2 混合请求 → 族选 `bioconductor`（BiocManager 能装 ggplot2）。
6. 纯 ggplot2 请求 → 族选 `cran`（不误伤）。
7. mock `_run_r_registry_install`，断言 CRAN/Bioconductor 镜像被正确注入 R 表达式。

---

## 涉及文件清单（整体）

| 文件 | 改动 | 战线 | 阶段 |
|---|---|---|---|
| `deploy/runtime/mirror-presets/*.condarc` | 新增 | W1 | A |
| `deploy/runtime/blueprint-re-r.yml` | 新增 | W1 | B |
| `scripts/build_release_bundle.sh` | 改：下 micromamba、`--with-r-cache` | W1 | A+B |
| `scripts/install_blueprint_re.sh` | 改：`provision_bundled_mamba`、`provision_bundled_r_runtime`、兜底 | W1 | A+B |
| `scripts/deploy_user_systemd.sh` | 改：`detect_conda_base` 认 micromamba、R env marker、env 白名单加镜像 key | W1+W2 | A+B |
| `backend/app/core/config.py` | 改：`default_conda_base_candidates` 加内置路径、新增 `cran_mirror`/`bioconductor_mirror` | W1+W2 | A+W2-3 |
| `backend/app/services/runtime_dependency_resolver_service.py` | 改：solver_error→fallback 降级、`_select_r_fallback_family`、`BIOCONDUCTOR_ONLY_PACKAGES` | W2 | W2-1/2 |
| `backend/app/services/manager_blueprint_tools.py` | 改：`_run_r_registry_install` 用 settings 镜像 | W2 | W2-3 |
| `backend/app/services/project_service.py` | 改：runtime dict 加 `source` | W1 | D |
| `backend/app/services/diagnostic_bundle_service.py` | 改：透传 `source` | W1 | D |
| `backend/tests/test_runtime_resolver_fallback.py` | 新增 | W2 | — |
| `backend/tests/test_runtime_provisioning.py` | 新增 | W1 | — |
| `docs/for_agent_install.md` | 改：补充说明 | W1 | D |

---

## 验证策略

### 单元/行为测试（CI）

1. `test_runtime_resolver_fallback.py`：W2 的 7 个用例（见上）。
2. `test_runtime_provisioning.py`：temp HOME 测试（仿 AGENTS.md 推荐的 behavior-based test）——`default_conda_base_candidates` 在只有内置 mamba 路径时返回内置路径；`find_conda_solver` 返回 micromamba；mock `micromamba create` 断言 `provision_bundled_r_runtime` 幂等。

### 端到端验收（手动，发布前）

干净 Ubuntu 22.04 容器（无 conda、无 R）：

1. 跑 `bash scripts/install_blueprint_re.sh`。
2. 验证 `~/.local/share/blueprint-re/mamba/bin/micromamba` 存在，`.condarc` 是清华镜像。
3. 验证 `BLUEPRINT_DEFAULT_R_RUNTIME=blueprint-re-r`，`micromamba run -n blueprint-re-r Rscript -e 'library(DESeq2)'` 成功。
4. 跑 `rnaseq-test1` 的 DESeq2 卡，应直接通过。
5. **W2 回归**：构造一个不在 `blueprint-re-r.yml` 里的包（如 `bioconductor-sva`），手动触发 resolver，验证：(a) 探测走镜像不超时；(b) 命中 Bioconductor 族；(c) 后台 job 用镜像成功安装；(d) 卡自动重试通过。
6. **超时回退**：临时断 conda 探测（或指向不可达地址），验证 resolver 降级为 fallback，Bioconductor 安装仍能成功（因 Bioconductor 镜像独立于 conda 探测）。
7. 海外模式：`BLUEPRINT_MIRROR_PRESET=default` 重复，验证官方源也 work。

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
| **R 包 ABI 与系统库** | conda env 自带 gfortran/BLAS，不依赖宿主系统库（决策 2 的理由） |
| **生信包清单不全** | 基础包覆盖 80%，缺的靠 W1-A 的 mamba + 镜像现场装 + W2 的 fallback 自动装 |
| **W2-1 误降级**（真不存在但探测报 solver_error） | 降级后走 fallback 安装，安装失败仍会报 `dependency_install_failed`（retry_hint=inspect_stderr），不静默成功；比当前"堵死在 manual"更好 |
| **W2-2 名单过时**（新 Bioconductor 包未收录） | 名单只兜底常见包；未收录的包若探测 not_found 仍走原 cran 收敛，BiocManager::install 对 cran 包也能成功，不会卡死 |
| **BiocManager 不尊重 BioC_mirror option** | 测试验证；若不行改为显式仓库 URL 或 `BiocManager::options` |
| **幂等/重复 provision 耗时** | deploy marker 保护；env 已存在跳过 |

---

## 与现有架构的契合

- **不碰** worker / bwrap / sandbox plan / 前端 / manager-agent 核心逻辑。只动分发层、runtime 探测层、resolver 的 fallback 判定、R 安装镜像。
- **复用** `find_conda_solver`（已支持 micromamba）、resolver 的 mamba 批量 repoquery 预取、deploy marker、后台 job/dedupe/retry_hint 全套。
- **遵守** AGENTS.md：路径不硬编码（镜像集中到预设文件/settings）、不引入硬限制、bwrap 沙箱不变、secrets 不进 git、改 Pydantic 后重生成 schemas。
- **不破坏** 用户自备 conda/R 路径——内置 runtime 是探测链路**最后兜底**；W2 的改动在"无 fallback 族或策略不允许"时完全保持原行为。

---

## 上线节奏

两条战线可并行，但建议：

1. **W1-A + W2 一起发**（核心闭环，~1 周）。W1-A 内置 mamba + 镜像让探测能成功，W2 让探测失败也能走 fallback + Bioconductor 镜像安装。发布 patch release。这两者配合就能让 DESeq2 这类场景**自动走通**（哪怕包不在基础清单）。
2. **W1-B 紧随**（2-3 天）。内置 R env 让核心流程真正零现场安装。发布 minor release，分标准/full 包。
3. **W1-C、W1-D 视反馈**。

### DESeq2 场景走通后的预期路径

修复后 `rnaseq-test1` 再跑 DESeq2 卡（两种情况都通）：

**情况 1：包在基础清单**（W1-B 后）
1. installer 已 provision `blueprint-re-r`，内含 `bioconductor-deseq2`。
2. 卡的 `r_env = "blueprint-re-r"`，`library(DESeq2)` 直接成功。

**情况 2：包不在基础清单**（W1-A + W2 后，如 `bioconductor-sva`）
1. executor 探测到缺包，报 `runtime_dependency_missing`。
2. Manager 调 `resolve_runtime_dependencies`，resolver 用内置 mamba + 清华镜像探测（秒级）。
3. 命中 conda 通道 → `fully_installable` → 后台 job `micromamba install` → 卡自动重试通过。
4. 或 conda 探测超时/未命中 → W2-1 降级为 `fallback_required` → W2-2 选 `bioconductor` 族 → 后台 job `BiocManager::install`（走 W2-3 清华 Bioconductor 镜像）→ 卡自动重试通过。

用户全程零干预、零终端操作。这正是"开箱即跑"的完整含义。
