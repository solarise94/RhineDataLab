# Manager-Agent Connection Refused 安装链路排查记录

## 范围

本文记录以下现场问题的安装/部署链路排查结论：

- 用户已配置 DeepSeek API Key
- 前端可正常打开
- Manager 发消息时报错：
  - `Pi manager request failed: [Errno 111] Connection refused`

目标是确认这类报错是否可能由安装程序或部署脚本的薄弱点放大，并整理后续修复方向。

## 结论

该报错通常不是 DeepSeek API 直接返回的业务错误，而是 backend 在向本地
`manager-agent` 转发请求时，目标地址没有进程监听。

当前默认链路为：

- backend 将 Manager 请求转发到 `BLUEPRINT_PI_MANAGER_URL`
- 默认值为 `http://127.0.0.1:18002`
- `manager-agent` 负责在该端口提供 `/chat-stream` 和 `/compact`

因此，`[Errno 111] Connection refused` 的直接含义是：

- backend 已发起本地 TCP 连接
- 但 `127.0.0.1:18002` 没有服务在监听

## 已确认的代码事实

### 1. backend 固定转发到本地 manager-agent

Files:

- `backend/app/core/config.py`
- `backend/app/services/manager_service.py`

Observed:

- `pi_manager_url` 默认值为 `http://127.0.0.1:18002`
- backend 在 `/chat-stream` 和 `/compact` 中都会向该地址发起 HTTP 请求
- 当 `urlopen()` 收到 `URLError` 时，会统一抛出：
  - `Pi manager request failed: {exc.reason}`

Implication:

- 如果 `manager-agent` 未启动、崩溃、端口错误、未监听，最终都会在前端表现为该错误。

### 2. manager-agent 启动时本身会做配置校验

Files:

- `manager-agent/src/server.js`

Observed:

- 启动校验会在以下场景报错：
  - 未配置 `MANAGER_AGENT_API_KEY` 或 `BLUEPRINT_DEEPSEEK_API_KEY`
  - `provider/model` 无法解析
  - DeepSeek base URL 非法

Implication:

- 即使用户“以为自己已经配置了 key”，只要运行时 `manager-agent.env` 没有正确值，
  sidecar 仍可能直接启动失败。

## 安装/部署链路中的风险点

### 1. backend 不要求 manager-agent 真正可用

Files:

- `deploy/systemd/blueprint-re-backend.service`

Observed:

- backend unit 只有：
  - `After=network.target blueprint-re-manager-agent.service`
- 没有：
  - `Requires=blueprint-re-manager-agent.service`

Impact:

- `manager-agent` 启动失败时，backend 仍可正常启动
- 前端与 `/healthz` 也可能都正常
- 问题会延迟到用户第一次发起 Manager 对话时才暴露

Assessment:

- 这是最容易把问题放大成现场“表面正常、聊天时报错”的关键点。

### 2. 部署健康检查未覆盖 manager-agent

Files:

- `scripts/deploy_release.sh`

Observed:

- 验证阶段只检查：
  - `http://127.0.0.1:18001/healthz`
  - `http://127.0.0.1:13001`
- 未检查：
  - `blueprint-re-manager-agent.service` 是否 `active`
  - `127.0.0.1:18002` 是否监听
  - `manager-agent` 是否能响应 HTTP

Impact:

- deploy 可以输出成功
- 但 manager 实际不可用
- 用户只会在首次聊天时遇到 `Connection refused`

Assessment:

- 这是第二个核心缺口。

### 3. 安装输入和运行时配置文件容易被混淆

Files:

- `scripts/install_blueprint_re.sh`
- `scripts/deploy_user_systemd.sh`
- `scripts/deploy_release.sh`

Observed:

- 安装脚本先写 repo 根目录 `.env`
- 实际运行时读取的是：
  - `~/.config/blueprint-re/backend.env`
  - `~/.config/blueprint-re/manager-agent.env`
- 安装脚本虽有提示“修改 `.env` 后需重新 deploy”，但现场仍很容易误操作

Typical failure mode:

- 用户修改了 repo `.env`
- 但没有重新执行 deploy
- 或者以为前一次配置已经同步到运行时 env
- 实际运行中的 `manager-agent.env` 仍为空值或旧值

Impact:

- `manager-agent` 可能因 key 缺失或旧配置启动失败
- 外在表现仍然是 `Connection refused`

### 4. 端口配置在多处硬编码，缺少一致性校验

Files:

- `scripts/install_blueprint_re.sh`
- `scripts/deploy_user_systemd.sh`
- `scripts/deploy_release.sh`
- `backend/app/core/config.py`

Observed:

- `18002` 在 backend 默认配置、deploy 写入逻辑、manager-agent env 中重复出现
- 当前仓库内它们是一致的
- 但没有统一的单一来源或部署期一致性断言

Impact:

- 后续如果有人只改动其中一处，极易造成 backend 与 manager-agent 端口错配
- 错配后的用户侧症状仍然是 `Connection refused`

Assessment:

- 这不是当前已确认的线上 root cause，但属于明显的未来回归风险。

### 5. manager-agent service 模板缺少前置自检

Files:

- `deploy/systemd/blueprint-re-manager-agent.service`

Observed:

- `ExecStart` 直接运行 `node src/server.js`
- 没有 `ExecStartPre` 级别的配置验证或端口探测

Impact:

- 启动失败时，问题信息主要沉淀在 `journalctl`
- deploy 阶段无法直接向操作者给出更清晰的失败原因

## 为什么“已经配置了 DeepSeek key”仍然会出错

常见误解是：

- “我已经填了 DeepSeek key，所以不应该是 manager 问题”

但实际链路是两段：

1. backend 先连本地 `manager-agent`
2. `manager-agent` 再用 DeepSeek key 请求模型

因此只要第 1 段失败，错误就会停留在本地连接阶段，甚至根本不会走到 DeepSeek API。

## 推荐修复方向

### P0

- 在 deploy 验证阶段新增 `manager-agent` 探活：
  - 检查 `systemctl --user status blueprint-re-manager-agent.service`
  - 检查 `127.0.0.1:18002` 监听
  - 最好补一个本地 HTTP 探测

- 如果 `manager-agent` 未启动成功，deploy 直接失败，不要只以 backend `/healthz` 为准。

### P1

- 在 deploy 成功摘要中明确打印：
  - `manager-agent` 状态
  - 运行时配置文件路径
  - “修改 repo `.env` 不会影响当前运行服务，需重新 deploy”

- 为 `manager-agent` service 增加更明确的启动前检查或更可读的失败日志。

### P2

- 收敛 `18002` 的配置来源，避免 backend 与 manager-agent 双边硬编码漂移
- 视产品策略决定 backend 是否应对 manager-agent 增加更强依赖约束

## 现场排查最短路径

推荐优先执行以下检查：

1. `systemctl --user status blueprint-re-manager-agent.service --no-pager`
2. `journalctl --user -u blueprint-re-manager-agent.service -n 120 --no-pager`
3. `ss -ltnp | grep 18002`
4. 核对：
   - `~/.config/blueprint-re/backend.env`
   - `~/.config/blueprint-re/manager-agent.env`

如果 `manager-agent` 未运行或 `18002` 未监听，则该报错可直接解释，不必先怀疑 DeepSeek API 本身。

## 总结

当前安装/部署程序最容易导致该问题的，不是单点配置写错，而是以下组合：

- `manager-agent` 可能因配置问题启动失败
- backend 与前端仍然可以正常上线
- deploy 健康检查未覆盖 `manager-agent`
- 问题最终延迟到用户首次聊天时才表现为 `Connection refused`

因此，这个问题在安装链路上是真实存在的可改进项，且优先级较高。
