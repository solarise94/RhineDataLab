# 文档导航

`docs/` 里既有产品蓝图，也有实现契约、问题复盘和发布记录。这里不展开细分，只给几个常用入口。

当前对外产品名以 `莱茵数据实验室（RhineDataLab）` 为准；文档中保留的
`blueprint-re` 目录名、systemd unit 名和历史 installer 产物名，主要是兼容现有
运行时布局与迁移阶段约定。

## 推荐先看

- [00_overview_blueprint.md](./00_overview_blueprint.md)
- [01_frontend_ui_blueprint.md](./01_frontend_ui_blueprint.md)
- [02_backend_implementation_blueprint.md](./02_backend_implementation_blueprint.md)
- [03_data_contracts_and_schemas.md](./03_data_contracts_and_schemas.md)

## 安装与部署

用户安装统一使用版本化自解压安装包 `rhinedatalab-<version>-linux-x86_64.sh` 作为入口。同一个安装包既能首次安装，也能升级已有安装：

```bash
sh rhinedatalab-<version>-linux-x86_64.sh
```

已有安装时，上述命令会自动进入升级流程。CI/运维脚本若要求“必须是升级”，可显式使用：

```bash
sh rhinedatalab-<version>-linux-x86_64.sh --upgrade
```

`--upgrade` 在没有已有安装时会报错，避免误操作。

也可以通过 release 固定入口 `install.sh` 自动解析、下载并校验对应版本的安装器：

```bash
curl -fsSL \
  https://github.com/solarise94/RhineDataLab/releases/latest/download/install.sh | \
  bash
```

当前用户版安装模型：

- 不需要 `sudo`，不默认调用 `apt`
- 安装到 `~/.local/share/blueprint-re/`、`~/.config/blueprint-re/` 和 `~/.config/systemd/user/`
- Python、Node.js、nginx、bubblewrap、git 由用户目录 runtime env 提供或引导安装
- backend 使用预构建 wheel 和 vendored Python dependency wheels
- frontend 使用预构建 Next.js standalone 输出
- manager-agent 使用 release payload 中的生产依赖
- 只对外暴露 nginx gateway 端口，backend/frontend/manager-agent 走本机内部端口

Provider key 不是安装 gate：

- `BLUEPRINT_DEEPSEEK_API_KEY` 是 provider-backed 功能的运行时凭据，不是安装/部署前置条件
- 缺 key 时安装仍应完成；backend `/healthz` 和 nginx 可访问是安装成功基线
- `manager-agent` 在缺少 provider credentials 时可以 degraded，后续配置凭据后再恢复相关能力

安装后基础验证：

```bash
systemctl --user status blueprint-re-nginx.service --no-pager
systemctl --user status blueprint-re-backend.service --no-pager
systemctl --user status blueprint-re-frontend.service --no-pager
systemctl --user status blueprint-re-manager-agent.service --no-pager
curl -fsS http://127.0.0.1:18001/healthz
curl -I http://127.0.0.1:13001
```

详细安装指引：

- [for_agent_install.md](./for_agent_install.md) — agent/开发者安装操作手册
- [51_user_mode_release_bundle_and_installer_plan.md](./51_user_mode_release_bundle_and_installer_plan.md) — user-mode release bundle 与 installer 设计
- [51-1_release_installer_credential_gate_followup.md](./51-1_release_installer_credential_gate_followup.md) — provider credential gate 后续策略
- [53_latest_installer_entry_and_release_automation_plan.md](./53_latest_installer_entry_and_release_automation_plan.md) — latest installer 固定入口与 release 自动化方案

## 最近重点链路

- [21_hot_path_performance_remediation.md](./21_hot_path_performance_remediation.md)
- [44_runtime_dependency_install_visibility_and_liveness_plan.md](./44_runtime_dependency_install_visibility_and_liveness_plan.md)
- [46_runtime_dependency_terminal_receipt_contract.md](./46_runtime_dependency_terminal_receipt_contract.md)
- [47_oaa2_dependency_terminal_and_card_scroll_remediation.md](./47_oaa2_dependency_terminal_and_card_scroll_remediation.md)

## 其他说明

- `00` 到 `40+` 编号文档基本按主题和时间逐步累积。
- 如果你是来查最近 bug 或修复方案，优先看编号靠后的文档。
- 如果你是第一次接手项目，先看“推荐先看”，再按需要深入。
