# Doc 71 — Manager Chat 单一真相源架构重构（设计稿，未实施）

> 状态：**设计稿，本轮只落文档，不实施**。本文记录目标架构、分阶段实施计划、回归手测清单与针对性单测清单，待评审通过后另起实施。
>
> 范围决策（已与负责人对齐）：
> - **激进版**：手动聊天路径也经由 `ChatStreamRelay` 组装+持久化；前端收敛到**单条** `EventSource` 订阅；删除 `fetch('/chat-stream')` 直读 SSE。
> - **仍推增量事件**：events SSE 继续推 `stream_event` 增量（保留流式体验），前端保留一个**瘦 reducer**（只渲染、不持久化）。
> - **直接替换无废弃期**：`/chat-stream` 语义原位替换，旧 `save_session` PUT 保留但前端面板不再调用。
> - 验收：现有后端单测全绿 + 前端 build 通过 + 端到端手测清单 + 加针对性单测。

---

## 1. 问题诊断：为什么"一个会话两个线程"

"两个线程"不是 manager-agent 内部的线程，而是 **chat session 的真相被切成两半，靠人工同步**。

### 1.1 现状：四条路径，两份真相

| 路径 | 谁组装 message + timeline | 谁持久化 | 前端怎么拿 |
|---|---|---|---|
| **手动聊天**（用户打字回车） | **前端** TS reducer `applyStreamEvent` | **前端** PUT `save_session` | `fetch('/chat-stream')` 直读 SSE |
| **auto/wake**（后台唤醒） | **后端** `ChatStreamRelay._apply_stream_payload` | **后端** `upsert_message` | `EventSource('/chat-sessions/{id}/events')` |
| **斜杠命令** `/auto ...` | 后端 `ManagerCommandService` | 后端 `append_messages` | 自造 SSE |
| **旁路写入**（dep_resolved / depjob_terminal） | 后端直写 | 后端 `upsert/append` | 只能靠 events |

关键引用：
- 前端 reducer：`frontend/components/manager-chat/ManagerChatPanel.tsx:1396-1779`（`applyStreamEvent`）
- 后端 reducer：`backend/app/services/chat_stream_relay.py:160-325`（`_apply_stream_payload`）
- 手动路径后端是纯代理：`backend/app/services/manager_service.py:129-181`（`stream_chat` 不触碰 `ChatSessionService`）
- relay 只在 auto 路径注入：`backend/app/api/deps.py:179-193`（`inject_wake_dispatch`，启动时一次性注入到 `ManagerAutoService`）

### 1.2 变扭的四个具体根因

1. **两套等价但不共享的 reducer**。Python `_apply_stream_payload` 与 TS `applyStreamEvent` 必须手动逐事件保持一致。当前事件词汇表一致，但**前端额外维护一套 `message.tools`（`ToolUseState[]`）影子状态，后端模型里根本没这个字段**（`backend/app/models/chat.py:81-90`）。它和 `timeline` 工具项分两条路径更新；user-stop 时一条翻 `error`、一条翻 `done`（`ManagerChatPanel.tsx:1982` vs `:1986`）——这是"工具调用不同步"的直接来源。

2. **手动路径下两份副本互相覆盖**。后端是纯代理，前端读 SSE 后还要把"前端以为的完整列表"PUT 回去（`saveSessionMutation`，`ManagerChatPanel.tsx:620-657`）。`save_session` 在 `base_revision` 匹配时**直接整体替换** `session.messages`（`chat_session_service.py:84-87`），能盖掉后台线程刚写的 `wake_response_*` / `depjob_terminal_*`。靠 `sessionMessagesSignature`（O(1) 哈希）+ `base_revision` 乐观锁兜底，race 高发。

3. **前端面板里两套接收机制并存**。`fetch('/chat-stream')` 直读 + `EventSource('/events')` 订阅，用 `activeAutoStreamMessagesRef` / `autoStreamSeqRef` / `remoteHydratingRef` 三个 ref 防冲突（`ManagerChatPanel.tsx:588-591, 956-999`），auto 时还要主动忽略 `/chat-stream` 的流。**这些 ref 存在的唯一理由就是两边都持可写副本**。

4. **工具状态错位**。`tool_call_id` / `assistant_turn_index` 在两条路径上各自演算 timeline；auto 后台跑时前端只收整条 message 快照，tool 卡片 running→done 经常错位或丢失。

### 1.3 设计原则

**让后端 `ChatSessionService` 成为 chat session 的唯一真相源，前端退化为纯视图 + 增量订阅。** 手动路径也走 relay，前端删掉自己的持久化职责和 `tools` 影子状态。

---

## 2. 目标架构（激进版）

### 2.1 拓扑

```
                                    ┌─────────────────────────────┐
  用户回车                            │  POST /chat-stream          │
─────────────►  ManagerChatPanel     │   (语义改写)                │
  只发 message + sessionId           │                             │
  不再本地组装消息                    │  1. append_messages([user]) │  ◄── 用户消息由后端写
  只订阅 events                       │  2. relay.run_to_session()  │
                                    │       └─ manager_service    │
        ┌───────────────────────────┤          .stream_chat (pi)   │
        │  EventSource               │  relay 组装+持久化+fanout   │
        │  /chat-sessions/{id}/events│  (upsert_message +          │
        ▼                            │   publish_stream_event)     │
  前端瘦 reducer                       └──────────────┬──────────────┘
  applyStreamEvent(瘦)                              │
  - 删 tools 影子状态                              │ pi /chat-stream
  - 删持久化分支                                    ▼
  - 只渲染                                  manager-agent（无状态）
```

四条路径在目标架构下统一到同一套持久化语义：

| 路径 | 目标行为 | 持久化 | 前端接收 |
|---|---|---|---|
| 手动聊天 | `/chat-stream` → append user + relay | 后端 relay | events SSE |
| auto/wake | `_dispatch_wake` → relay（不变） | 后端 relay | events SSE |
| 斜杠命令 | `ManagerCommandService`（不变） | 后端 append | events SSE |
| 旁路写入 | dep_resolved / depjob_terminal（不变） | 后端 append/upsert | events SSE |

**关键不变量**：所有对 session messages 的写入都发生在后端；前端**只读、只渲染**。

### 2.2 后端改动

#### 2.2.1 `/chat-stream` 端点改写（`backend/app/api/chat.py:55-88`）

把非斜杠命令分支从"纯代理 `manager_service.stream_chat`"改为"写用户消息 + 跑 relay"：

```python
@router.post("/chat-stream")
def chat_stream(
    project_id: str,
    request: ChatRequest,
    manager_service: ManagerService = Depends(get_manager_service),
    manager_command_service = Depends(get_manager_command_service),
    chat_session_service = Depends(get_chat_session_service),
    chat_stream_relay = Depends(get_chat_stream_relay),   # 新增依赖
) -> StreamingResponse:
    is_cmd, cmd_type, obj = parse_slash_command(request.message)
    if is_cmd:
        return StreamingResponse(
            manager_command_service.handle_auto_command_stream(project_id, request, cmd_type, obj),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    # ── 新增：用户消息由后端写入 ──
    session_id = _ensure_session(project_id, request, chat_session_service)
    user_message = _build_user_message(request)            # id 由后端生成
    chat_session_service.append_messages(project_id, session_id, [user_message])

    # ── 改写：跑 relay，relay 负责组装+持久化+fanout ──
    manager_message_id = f"mgr_{uuid4().hex[:12]}"
    return StreamingResponse(
        chat_stream_relay.stream_to_http(project_id, session_id, request,
                                         message_id=manager_message_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
```

注意：
- `request.session_id` 为空时由后端创建会话并回填（替代今天前端在创建会话后才发消息的流程）。
- 用户消息 `id` 由后端生成，前端不再需要 `userMessageId`。

#### 2.2.2 `ChatStreamRelay` 新增 HTTP 流出口（`backend/app/services/chat_stream_relay.py`）

relay 当前只有 `run_to_session`（auto 路径用，同步阻塞返回 `ChatResponse`）。手动路径需要一个**把内部 `stream_event` 同时 fanout 到 HTTP SSE 响应体**的出口。新增方法（不改动现有 `run_to_session`，auto 路径零回归）：

```python
def stream_to_http(
    self, project_id: str, session_id: str, request: ChatRequest,
    *, message_id: str, initial_thinking: str | None = None,
) -> Iterator[bytes]:
    """与 run_to_session 共享组装/持久化逻辑，但把 stream_event
    同时回灌成 HTTP SSE 字节流（data: {...}\\n\\n）。

    复用同一套 _apply_stream_payload / persist / publish 逻辑，
    保证手动路径与 auto 路径产出**完全一致**的 session 真相。
    """
```

实现要点（共享 `run_to_session` 的循环体，把每次 `publish_stream_event` 也 yield 一份到 HTTP body）。为避免逻辑分叉，**重构 `run_to_session` 的循环为内部生成器**，两个出口都消费它：
- `run_to_session`：消费生成器，只做 session 写入 + `publish_stream_event`（给 events SSE）。
- `stream_to_http`：消费同一生成器，额外把 `stream_event` 序列化成 `data: {...}\n\n` yield 到 HTTP 响应。

这保证两条路径**共享同一个 reducer**，从根上消除根因 #1（两套 reducer）。

#### 2.2.3 relay 依赖注入（`backend/app/api/deps.py`）

新增工厂并参与启动注入：

```python
@lru_cache
def get_chat_stream_relay() -> ChatStreamRelay:
    return ChatStreamRelay(get_chat_session_service(), get_manager_service())
```

`inject_wake_dispatch`（`deps.py:179-193`）改为复用 `get_chat_stream_relay()`，保证全局唯一 relay 实例。

#### 2.2.4 events SSE 的"落后补齐"（**激进版能正常工作的前提**）

当前 events 扇出是 `Queue(maxsize=256)` 的尽力推送，慢消费者会丢（`chat_session_service.py:162, 244-251`）。手动聊天实时性要求高，丢 `text_delta` 会破坏渲染。设计补齐机制：

1. **`message_upsert` 事件始终带 `revision`**（现状已带，`chat_session_service.py:150-157`）。前端在 `message_upsert` 时校验：若收到的 `revision` 比本地期望大 >1，说明有中间事件丢了 → 主动 `GET /chat-sessions/{id}` 拉全量快照对齐。
2. **`stream_event` 的 `seq`** 已是单调递增（relay 内 `stream_seq`，`chat_stream_relay.py:40,47,67`）。前端按 `message_id` 维护期望 `seq`，发现 gap 即触发快照对齐。
3. **重连语义**：前端连接/重连 events 时，先 `GET /chat-sessions/{id}` 拉快照建立基线，再订阅 events（`subscribe_events` 内部 15s 心跳保持，`chat_session_service.py:169-171`）。这是"最终一致"兜底——即便中间丢事件，下一次 `message_upsert`（relay 周期性持久化 0.75s，`chat_stream_relay.py:80-82`）会把快照纠正回来。

> 这条不需要立即实现为完美的"零丢失"，而是保证"**最坏情况下 0.75s 内被下一次持久化快照纠正**"——这是 single-source-of-truth 相对现状的根本优势：真相只有一份，丢事件只会导致短暂渲染滞后，不会导致状态分叉。

#### 2.2.5 斜杠命令统一 events fanout

`ManagerCommandService.handle_auto_command_stream`（`manager_command_service.py:31-186`）当前自造 SSE 直接写到 HTTP 响应体。改后：把 `append_messages` 后的 `message_upsert` 也通过 `publish_stream_event` fanout（让 events SSE 也能收到），HTTP 响应体可保留或改为转发。优先级低，可与手动路径合并时一并处理（见 §3 阶段 3）。

### 2.3 前端改动

#### 2.3.1 删除 `api.streamChat` 的 fetch 直读（`frontend/lib/api.ts:488-555`）

替换为一个不返回流、只触发后端处理的 POST（fire-and-forget，或保留返回确认）。前端不再读取 `response.body.getReader()`。

#### 2.3.2 `submit()` 简化（`ManagerChatPanel.tsx:1879-2036`）

删掉本地组装用户消息 + manager 占位消息（`:1901-1914`），改为：

```ts
async function submit() {
  if (!draft.trim() || busy || !sessionId) return;
  const text = draft.trim();
  if (text === "/compact") { await runManualCompaction(); return; }
  setDraft(""); setBusy(true); setError(null);
  try {
    // 只发文本 + sessionId，后端负责写消息 + 跑 relay
    await api.sendChat(projectId, text, thinkingEffort, chatContext, sessionId);
    // 实际渲染由 events SSE 驱动，本地不组装任何消息
  } catch (e) { setError(...); }
  finally { setBusy(false); }
}
```

`busy` 的清除改为订阅到该 turn 的 `done`/`error` 事件时（events 推 `response`/`error`）。

#### 2.3.3 events 订阅成为唯一接收通道

保留并简化 `ManagerChatPanel.tsx:920-1030` 的 EventSource effect：
- **删除** `stream_event` 只在 `isAutoOwnerSession` 时才处理的 gate（`:956-958`）——现在手动路径也走 events。
- **删除** `activeAutoStreamMessagesRef` / `autoStreamSeqRef` / `remoteHydratingRef`（`:588-589,591`）——它们的唯一用途是隔离两套接收机制，机制合并后无存在必要。
- 保留 `message_upsert`（全量快照对齐）和 `stream_event`（增量渲染）两个分支。

#### 2.3.4 瘦化 `applyStreamEvent`（`ManagerChatPanel.tsx:1396-1779`）

保留事件→timeline 的渲染逻辑，但删除：
- `message.tools`（`ToolUseState[]`）影子状态及其全部更新路径（`:1555-1578, 1579-1610, 1611-1654, 1736-1743`）。timeline 工具项是唯一渲染源（`renderTimelineItem` 只读 `timeline`，`:2241-2305`），`tools` 数组当前**根本不被渲染**。
- 任何持久化相关副作用（`:1655-1665` 的 `syncProposal` 写 react-query 缓存保留，但 PUT 回写删除）。
- `settleInterruptedTools`（`:1982`）—— timeline 的 `settleRunningTimelineItems`（`:1986`）统一负责。

#### 2.3.5 删除 auto-save effect（`ManagerChatPanel.tsx:893-918`）

整个 debounced PUT `saveSessionMutation` 删除。`saveSessionMutation`（`:620-657`）、`sessionMessagesSignature`（`:509-517`）、`lastSavedSignatureRef`（`:596`）、`sessionRevisionRef`（`:597`）一并删除。`remoteHydratingRef`（`:591`）删除。

#### 2.3.6 compact 路径

`runManualCompaction`（`:1848`）调 `api.compactChatSession`，结果当前在前端 `finalizeCompaction`（`:1224-1243`）里截断本地列表。改后：`/chat-compact` 后端也应把 compact timeline item 写进 session（今天后端 `compact_chat_session` 是纯代理不写，`manager_service.py:183-219`），前端只订阅 `message_upsert` 渲染。compact 是已有功能的小修，列在阶段 3。

### 2.4 manager-agent 改动

**无。** manager-agent 已是无状态、纯函数式（`server.js:3142-3195` `buildSessionEntries` 从 payload 重建，每次 `/chat-stream` 全新 Agent）。它不知道 session 真相在哪，只消费后端传入的 `session_messages`。后端改不改 session 真相来源，对它透明。

唯一需要保证的是：后端传给 manager-agent 的 `session_messages`（`manager_service.py:142`，来自 `request.messages`）仍然完整准确。改后这是由 relay 持久化的 session 派生，**比今天前端 PUT 的版本更可信**。

---

## 3. 分阶段实施计划

> 本文档只记录计划，不执行。实施时按阶段提交，每阶段独立可验证。

### 阶段 0：后端 relay 出口与 DI（不改前端，零行为变化）
1. `ChatStreamRelay` 重构循环为内部生成器；新增 `stream_to_http`（`chat_stream_relay.py`）。
2. `deps.py` 新增 `get_chat_stream_relay`；`inject_wake_dispatch` 复用之。
3. **不**改 `/chat-stream` 端点，**不**改前端。
4. 验证：现有后端单测全绿（尤其 `chat_stream_relay` / `chat_session_service` 相关）；新增单测：`stream_to_http` 与 `run_to_session` 在相同输入下产出相同 session 真相（见 §5.1）。

### 阶段 1：手动路径走 relay（后端先行，前端可后跟）
1. `chat.py` 的 `/chat-stream` 非命令分支改为：`append_messages([user])` + `stream_to_http`。
2. 新增 `_ensure_session`（`session_id` 空时创建并回填）+ `_build_user_message`（后端生成 id）。
3. 验证：用 curl 直接打 `/chat-stream`，确认 session 被正确写入用户+manager 消息、events SSE 能收到 `stream_event`。
4. **此阶段前端仍走旧 fetch 直读**——但后端现在同时 fanout events，前端会收到双份（旧 fetch + events）。**这是过渡态，必须与阶段 2 同 PR 或紧接合并**，不长期停留。

### 阶段 2：前端收敛到单条 EventSource（与阶段 1 同批）
1. `api.ts` 删除 `streamChat` 的 reader 逻辑，改为 `sendChat`（fire POST）。
2. `ManagerChatPanel.tsx` `submit()` 简化（删本地组装）。
3. events effect 删除 auto-only gate 和三个防冲突 ref。
4. `applyStreamEvent` 删 `tools` 影子状态。
5. 删除 auto-save effect + `saveSessionMutation` + signature/revision ref。
6. 验证：`cd frontend && npm run build` 通过；§4 手测清单全过。

### 阶段 3：收尾与旁路统一
1. compact 路径：`/chat-compact` 后端写 compact timeline item 进 session；前端 `finalizeCompaction` 简化为只订阅。
2. 斜杠命令 `handle_auto_command_stream` 的 `message_upsert` 也走 `publish_stream_event`。
3. 清理：删除 `api.appendChatSessionMessages`（`api.ts:344-354`，已确认面板未用）、旧 `save_session` PUT 的 deprecated 标记。
4. 验证：全量手测 + 单测。

### 回滚策略
- 阶段 0/1 后端可独立回滚（revert 端点改写即可，relay 新增方法不影响旧路径）。
- 阶段 2 前端必须与阶段 1 同批合并，避免双份事件过渡态。
- 整体 git revert 即可恢复旧架构，无数据迁移、无 schema 变更。

---

## 4. 端到端手测清单（实施后由负责人按单验证）

> 每条标注预期：**后端 session 文件应包含什么消息**（可在 `workspace/<project>/.blueprint/chat_sessions.json` 或等价存储核对）。

### 4.1 手动聊天
- [ ] **M1** 输入"帮我看看这个项目"，回车。预期：前端出现 1 条 user + 1 条 manager 消息；timeline 含 thinking/text/tool 项；后端 session 含相同两条，`revision` 单调递增。
- [ ] **M2** 流式中刷新页面（F5）。预期：刷新后消息完整恢复，无重复、无丢失；无残留 running 状态的 tool 卡片。
- [ ] **M3** 连续发 3 条消息。预期：3 条 user + 3 条 manager，顺序正确；后端 session 与前端一致。
- [ ] **M4** 发送带附件（卡片/资产）的消息。预期：附件随 user 消息进 session（`attachments` 字段）；manager 回复正常。

### 4.2 中途停止 / 断连
- [ ] **S1** 流式中点"停止"。预期：manager 消息 settle 为 `done`（不是 error）；timeline 里进行中的 tool 项 settle 为 `done`（**不**是 error——这是当前 `tools` 翻 error 的 bug 验证点）；后端 session 与前端一致。
- [ ] **S2** 流式中关闭浏览器标签再重开。预期：后端 relay 因断连把消息 settle（参考 `manager_service.py:171-173` 的 socket 错误路径），重开后消息状态正确，无残留 thinking。
- [ ] **S3** manager 超时（模拟 pi 慢响应 > `manager_timeout_seconds`）。预期：前端收到 error 事件，manager 消息 `state: error`。

### 4.3 工具调用 timeline
- [ ] **T1** 触发一个会调工具的提问。预期：tool_start→tool_end→tool_report 完整出现；running 时显示计时器（`RunningToolTimer`），结束后显示静态标签；状态无 error/done 错位。
- [ ] **T2** 触发会连续调多个工具的提问（多轮 tool-use）。预期：每个工具独立的 timeline 项，`tool_call_id` 区分；`assistant_turn_index` 递增不导致 thinking/text 项 id 冲突。
- [ ] **T3** 工具报错（模拟后端工具返回 error）。预期：tool 项 `status: error`，manager 后续回复能引用错误。

### 4.4 auto/wake（回归——此路径基本不动）
- [ ] **A1** 启用 auto，等一次 workboard wake。预期：出现 `wake_response_*` 消息，timeline 完整；后端 session 含该消息。
- [ ] **A2** auto 运行中追加指令（`addManagerAutoDirective`）。预期：出现 ack 消息；不影响正在进行的 wake turn。
- [ ] **A3** auto 时手动发消息（非 auto owner 场景）。预期：手动消息走新路径，与 wake 消息不互相覆盖。

### 4.5 斜杠命令
- [ ] **C1** 输入 `/auto`。预期：出现 `cmd_usr_*` + `cmd_mgr_*` 消息；后端 session 含两条；events SSE 也能收到（阶段 3 后）。
- [ ] **C2** 输入 `/compact`。预期：出现 compact timeline item，之前的消息按 `first_kept_message_id` 截断；后端 session 含 compact item（阶段 3 后）。

### 4.6 旁路写入
- [ ] **P1** 触发一个 runtime-dependency job 终态。预期：出现 `depjob_terminal_*` 消息；**不**被前端 PUT 覆盖（这是根因 #2 的验证点——改后前端不再 PUT）。
- [ ] **P2** 手动 mark runtime dependency resolved。预期：出现 `dep_resolved_*` 消息。

### 4.7 跨会话/多标签
- [ ] **X1** 同一用户开两个标签看同一会话。预期：一个标签发消息，另一个标签通过 events SSE 实时看到（today 因双份副本 + PUT 竞争易错）。
- [ ] **X2** 切换会话。预期：无残留流、无错误 settle。

---

## 5. 针对性单测清单

### 5.1 后端（`backend/tests/`）
- [ ] **U1** `test_chat_stream_relay_http_matches_run_to_session`：同一输入，`stream_to_http` 与 `run_to_session` 产出的最终 session messages **逐字段相等**（含 timeline、state、thinking、token_usage）。这是"单一 reducer"不变量的直接断言。
- [ ] **U2** `test_chat_stream_endpoint_writes_user_message`：`POST /chat-stream` 后，session 含 1 条 user + 1 条 manager，user 消息 id 由后端生成、`attachments` 透传。
- [ ] **U3** `test_chat_stream_no_frontend_put_needed`：模拟前端**不**调 `PUT /chat-sessions/{id}`，session 仍正确持久化（验证前端不再需要回写）。
- [ ] **U4** `test_chat_stream_background_write_not_overwritten`：手动 turn 进行中，后台线程写一条 `depjob_terminal_*`；turn 结束后该消息仍在（验证根因 #2 修复——后端 relay 用 `upsert`/`append`，不再有整体替换的 PUT）。
- [ ] **U5** `test_events_sse_carries_revision`：`message_upsert` 事件 `revision` 与 session.revision 一致；`stream_event` 的 `seq` 单调。
- [ ] **U6** `test_events_sse_snapshot_recovery`：丢弃中间 `stream_event`，下一次 `message_upsert` 后前端基线应能对齐（模拟：只收 `message_upsert` 不收 `stream_event`，断言最终 timeline 正确）。
- [ ] **U7** `test_chat_stream_abort_settles_done_not_error`：模拟客户端断连（`manager_service` socket 错误路径），manager 消息 settle 为 `done`，timeline 工具项 settle 为 `done`（验证 §S1 的 bug 修复）。

### 5.2 前端
- [ ] **F1** 类型与编译：`cd frontend && npm run build` 无错（瘦 reducer 删除 `tools` 后，所有引用点清理干净）。
- [ ] **F2**（可选，若前端有测试基建）`applyStreamEvent` 瘦版：对相同事件序列，产出的 timeline 与后端 `_apply_stream_payload` 序列化结果一致（跨语言对拍，可用固定事件 fixture）。

---

## 6. 风险与权衡

| 风险 | 影响 | 缓解 |
|---|---|---|
| events SSE 丢事件导致渲染 gap | 手动聊天流式感弱 | §2.2.4 落后补齐：最坏 0.75s 被 `message_upsert` 快照纠正；这是 single-source 的根本优势（丢事件=滞后，非分叉） |
| 阶段 1/2 过渡态双份事件 | 短期前端收到双份 | 阶段 1+2 强制同 PR 合并，不长期停留 |
| 手动路径 relay 化增加后端单点负载 | relay 持久化频率 0.75s | 现有 relay 已在 auto 路径验证此频率可接受；手动并发量低 |
| `session_id` 空时由后端创建，前端需同步 | 首条消息会话归属 | 阶段 1 `_ensure_session` 返回 session_id，前端订阅 events 前先拿快照 |
| 前端 `tools` 影子状态被多处隐式引用 | 删除后遗漏引用 | 阶段 2 build 验证 + grep 确认（`renderTimelineItem` 只读 `timeline`，已核实 `:2241-2305`） |

## 7. 不在本次范围
- 不改 `ChatSessionMessage` / `ChatSessionMessageTimelineItem` 模型字段（无 schema 变更，免重新生成 schema JSON）。
- 不改 manager-agent（无状态，透明）。
- 不引入 WebSocket（events SSE 够用，且已是现有订阅契约）。
- 不改 `workspace/` 存储格式。

## 8. 关键文件清单（实施时按此定位）
- `backend/app/api/chat.py:55-88`（`/chat-stream` 端点改写）
- `backend/app/services/chat_stream_relay.py`（新增 `stream_to_http`，重构循环）
- `backend/app/api/deps.py:179-193`（`get_chat_stream_relay` + `inject_wake_dispatch` 复用）
- `backend/app/services/chat_session_service.py:84-87,161-198`（`save_session` 合并语义、events fanout——大部分不动）
- `frontend/lib/api.ts:488-555`（删 `streamChat` reader → `sendChat`）
- `frontend/components/manager-chat/ManagerChatPanel.tsx:580-605,893-1030,1396-1779,1879-2036`（ref/effect/reducer/submit 四处瘦身）
- `backend/app/services/manager_command_service.py:31-186`（阶段 3 统一 fanout）
- `backend/app/services/manager_service.py:183-219`（阶段 3 compact 写 session）

## 9. 验收标准（实施完成的定义）
1. `scripts/run_backend_tests.sh` 全绿，含 §5.1 全部新增单测。
2. `cd frontend && npm run build` 无错。
3. §4 手测清单全部通过（负责人按单验证）。
4. 代码中不再存在：前端 `tools` 影子状态、前端 `saveSessionMutation` 回写、`activeAutoStreamMessagesRef` / `autoStreamSeqRef` / `remoteHydratingRef`、`api.streamChat` 的 reader 逻辑。

---

## 附：决策记录
- **为什么仍推增量事件而非 message 快照**：保留流式增量体验（用户决定）；瘦 reducer 维护成本远低于当前双 reducer + 三 ref 的同步成本。
- **为什么直接替换无废弃期**：双写期反而引入"哪份真相对"的新歧义（用户决定）；git revert 足够回滚。
- **为什么 manager-agent 不动**：它已是无状态纯函数，架构问题不在它，在后端手动路径的"纯代理"设计。

---

## 10. 评审修订（v2）

> 评审指出五个风险点，全部成立。本节是方案的权威修订——§1-§9 的方向不变，但以下五条覆盖原方案的对应细节，实施时**以本节为准**。

### 10.1 【高】生成器共享的异常传播复杂度 → 阶段 0 先独立实现

**评审点**：§2.2.2 提议把 `run_to_session`（`chat_stream_relay.py:25-97`）的循环体重构成内部生成器让 `stream_to_http` 共享。但该循环里有交织的逻辑：异常处理链（socket 错误 / 超时 / 通用异常分别 settle 不同状态）、持久化节流（0.75s throttle + 结构性事件立即持久化）、异步 publish 与同步 persist 的交错。抽成生成器后，**yield 点决定了异常在哪一侧 raise**，而 `stream_to_http` 的消费者（FastAPI `StreamingResponse`）在客户端断连时会停止迭代——生成器需要能感知并 settle 消息，这与当前 `run_to_session` 的 except-settle 等价但跨了 generator 边界，调试难度显著增加。

**修订**：阶段 0 **先写 `stream_to_http` 的独立实现，允许少量重复**（复用 `_apply_stream_payload` / `_initial_stream_message` / `settle_stream_message` 这些纯函数，但不强求共享循环体）。等 §5.1 的 **U1（双出口产出相等）单测跑通**，确认两条路径语义确实等价后，**再**考虑提取共享生成器。原则：**不为 DRY 引入更难调试的抽象**。

- §2.2.2 的"重构循环为内部生成器"从**阶段 0 必做**降级为**阶段 0 可选 / 阶段 1+ 之后视情况提取**。
- 阶段 0 的唯一硬目标是：`stream_to_http` 独立实现 + U1 单测通过。
- 共享提取如果做，必须额外补一条单测：`test_generator_exception_propagation`——断言 HTTP 消费者中途停止迭代时，session 消息仍被正确 settle。

### 10.2 【高】首条消息即时可见性丧失 → 前端乐观追加用户消息（仅渲染）

**评审点**：§2.3.2 让 `submit()` 不再本地组装消息，依赖 events SSE 推 `message_upsert` 才出现用户消息。网络抖动或 events 队列延迟时，用户回车后几百毫秒才看到自己说的话——UX 退化。

**修订**：前端 `submit()` **保留对用户消息的乐观渲染**，但**不做任何持久化**：

```ts
async function submit() {
  if (!draft.trim() || busy || !sessionId) return;
  const text = draft.trim();
  if (text === "/compact") { await runManualCompaction(); return; }
  const userMessageId = createMessageId();  // 仅用于乐观渲染的 join key
  // 乐观追加：只渲染，不 PUT
  setMessages((prev) => [...prev, { id: userMessageId, role: "user", content: text,
    attachments: messageAttachments, state: "done",
    timeline: [{ id: `${userMessageId}_text`, kind: "text", content: text, status: "done" }] }]);
  setDraft(""); setBusy(true); setError(null);
  try {
    await api.sendChat(projectId, text, thinkingEffort, chatContext, sessionId, userMessageId);
    // 后端会用同一 userMessageId 写入（见 §10.5），events 推回的 message_upsert 经
    // mergeChatMessagesById 用 id 对齐覆盖乐观版本（attachments 等字段以后端为准）
  } catch (e) { setError(...); }
  finally { setBusy(false); }  // busy 实际在收到该 turn 的 done/error 事件时清
}
```

**这不破坏单一真相源**——前端不 PUT 回去，后端是唯一持久化方；乐观消息只是渲染占位，被后端 `message_upsert` 用相同 id 对齐替换。manager 占位消息**不**乐观追加（它依赖流式增量，乐观一个空 manager 消息没有 UX 价值，反而要先 settle 掉）。

- §2.3.2 的代码骨架以此为准（原骨架删掉了乐观追加，此处恢复用户消息的乐观渲染）。
- 新增约束：`api.sendChat` 必须把前端生成的 `userMessageId` 透传给后端，后端 `append_messages` 用这个 id 写入（见 §10.5 的 ID 回填协议），保证乐观版本与权威版本能按 id 合并。

### 10.3 【中】阶段 1+2 原子合并窗口 → 加 feature flag

**评审点**：§3 说阶段 1+2 必须同 PR，但双向风险不对称——阶段 1 先合并 → 前端双份数据重复渲染；阶段 2 先合并 → 手动聊天完全无响应。原子合并也意味着回滚必须原子，"出问题 revert 阶段 2 必须同时 revert 阶段 1"。文档没给缓解方案。

**修订**：引入 feature flag `BLUEPRINT_CHAT_SINGLE_SOURCE`（后端 `Settings` 新增 bool 字段，默认 `false`；部署时通过 `backend.env` 白名单控制，遵循 AGENTS.md 的 deploy 配置规则）：

- **后端** `/chat-stream`：flag=true 走 relay（append user + `stream_to_http`），flag=false 走旧纯代理路径。两条路径并存于代码，靠 flag 切换。
- **前端**：通过一个 `/healthz` 或 config 端点暴露的 flag（或单独 `GET /chat-config`）选择接收方式——flag=true 只订阅 events，flag=false 走旧 fetch 直读。
- **回滚**：出问题时把 flag 翻回 `false` 并重启后端，**不改代码、不 revert**。比 git revert 快且无合并冲突风险。

阶段划分相应调整：

- **阶段 0**：`stream_to_http` 独立实现 + U1 单测（flag=false，零行为变化）。
- **阶段 1**：后端 relay 路径 + flag 开关（flag=false 上线，代码就位但不启用）。
- **阶段 2**：前端条件接收 + flag 开关（flag=false 上线）。
- **阶段 3**：翻 flag=true 灰度验证 → 全量。compact/斜杠统一也在此阶段。
- **阶段 4**（清理）：稳定后删除旧纯代理路径 + flag + 前端旧接收代码。

flag 本身是过渡债务，但比"原子合并 + 原子回滚"的风险低得多。§7"不引入 WebSocket"的范围约束不变——flag 是临时机制，不属于架构。

### 10.4 【中】用户停止时工具 settle 状态 → 新增 `interrupted` 语义

**评审点**：§2.3.4 / §4-S1 提议把停止时的工具 settle 统一为 `done`。但 `settleInterruptedTools`（`ManagerChatPanel.tsx:164-173`）当前把 running 工具翻 `error`，`settleRunningTimelineItems(..., "done")`（`:175-189`，调用 `:1986`）翻 `done`。评审正确指出：用户主动停止时工具被中断、没有正常返回，标 `done` 会**误导用户以为工具成功完成**。

**修订**：新增 `interrupted` 状态（而非二选一 done/error）。需联动改动：

1. **后端模型** `ChatSessionMessageTimelineItem.status`（`backend/app/models/chat.py:64-78`）：当前是 `str | None`，实践值 `running|done|error`。新增合法值 `interrupted`。**这是本次唯一需要的模型语义扩展**（仍是 string，无 schema 破坏，免重新生成 schema JSON——只是新增一个约定值）。
2. **后端 relay** `settle_stream_message`（`chat_stream_relay.py`）：断连/停止路径 settle 为 `interrupted` 而非 `done`。需确认 relay 的断连分支当前 settle 成什么。
3. **前端** `settleRunningTimelineItems` 停止路径（`:1986`）改传 `"interrupted"`；`renderTimelineItem`（`:2241-2305`）为 `interrupted` 增加一个视觉态（如灰色"已中断"标签，区别于 done 的完成态和 error 的报错态）。
4. **单测 U7**（§5.1）改为断言 `interrupted` 而非 `done`：`test_chat_stream_abort_settles_interrupted_not_error`——停止/断连后工具项 status 为 `interrupted`，message state 为 `done`（消息本身结束了，工具是被中断的，两者语义不同）。

§4-S1 手测预期相应改为："manager 消息 settle 为 `done`；timeline 里进行中的 tool 项 settle 为 `interrupted`（灰色'已中断'），**不是 done 也不是 error**"。

### 10.5 【中】新建 session 的 ID 回填时序 → 明确 POST 响应协议

**评审点**：§2.2.1 让前端可以带空 `session_id` 发消息，后端创建 session 并回填。但前端需要 session_id 来更新 URL/侧边栏高亮 + 订阅 events。而当前 `stream_event`/`message_upsert` 的 payload **不带 session_id 字段**（已核实 `chat_session_service.py:124-128, 150-157`），前端无从得知新建的是哪个 session。

**修订**：明确协议——`sendChat` 的 **POST 响应头/体**返回 session_id，**不**依赖 SSE 流携带：

```python
# /chat-stream 端点（flag=true 路径）
session_id = _ensure_session(project_id, request, chat_session_service)
# ... append user + stream_to_http
return StreamingResponse(
    stream_to_http(...),
    headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "X-Blueprint-Session-Id": session_id,   # 新增：回填 session_id
    },
)
```

前端时序：

1. `submit()` 时若 `sessionId` 为空，先乐观渲染用户消息（§10.2）。
2. `api.sendChat` 发 POST，**从响应头读 `X-Blueprint-Session-Id`**。
3. 拿到 session_id 后：更新 URL / 侧边栏高亮 / `useChatSession` query key / **此时才订阅 events SSE**。
4. events 订阅后，之前错过的 `message_upsert` 靠 `GET /chat-sessions/{id}` 拉快照补齐（§2.2.4 的重连语义在此复用）。

边界情况：POST 响应头到达前，events 还没订阅——这段时间的 manager 流式增量会丢，但首次 `message_upsert`（relay 0.75s 持久化）会快照补齐。对首条消息可接受（用户刚发完，几百毫秒内 manager 开始回复，快照纠正延迟感知低）。

**`sendChat` 签名**（§10.2 已引用）：`sendChat(projectId, text, thinkingEffort, context, sessionId?, userMessageId?)` —— `sessionId` 可空（触发后端创建），`userMessageId` 透传（§10.2 乐观渲染对齐用）。

### 10.6 修订汇总表

| 评审风险 | 等级 | 修订动作 | 影响章节 |
|---|---|---|---|
| 生成器异常传播 | 高 | 阶段 0 先独立实现，U1 通过后再提取共享 | §2.2.2, §3 阶段 0 |
| 首条消息即时性 | 高 | 前端乐观追加用户消息（仅渲染不持久化） | §2.3.2 |
| 原子合并窗口 | 中 | feature flag `BLUEPRINT_CHAT_SINGLE_SOURCE` | §3 全部阶段重排 |
| 停止时工具状态 | 中 | 新增 `interrupted` 状态（非 done/error） | §2.3.4, §4-S1, §5.1 U7, 模型 |
| session ID 回填 | 中 | POST 响应头 `X-Blueprint-Session-Id` + 前端先拿 id 再订阅 | §2.2.1, §2.3.2 |

### 10.7 修订后的关键验证点

评审的结论"U1 是最关键验证点"完全正确，强化为**阶段 0 的门禁**：

- **U1 写不出来，或写出来发现两出口产出不等** → 方案需重新审视（可能意味着 reducer 抽象方式错了，或事件语义有未发现的分叉）。这是 go/no-go 的硬门槛，不是普通单测。
- U1 通过后，§10.1 的共享提取才是安全的。
- flag（§10.3）让 U1 的验证可以在生产旁路进行（flag=false 时新代码不生效），进一步降低风险。

其余单测优先级调整：U7 改名并改断言为 `interrupted`（§10.4）；新增 U8 `test_chat_stream_returns_session_id_header` 验证 §10.5 的响应头协议。

### 10.8 【补充评审】`interrupted` 类型的 TypeScript 联合类型链 + CSS

**评审点**：§10.4 新增 `interrupted` 状态，但前端 `ToolState`（`ManagerChatPanel.tsx:28`）当前是 `"running" | "done" | "error"`，`settleRunningTimelineItems` 签名（`:177`）是 `Extract<ToolState, "done" | "error">`——直接传 `"interrupted"` 会编译报错。

**修订（实施必做清单）**：
1. `ToolState` 扩展为 `"running" | "done" | "error" | "interrupted"`（`ManagerChatPanel.tsx:28`）。
2. `settleRunningTimelineItems` 签名改为 `Extract<ToolState, "done" | "error" | "interrupted">`（`:175-177`）。
3. `renderTimelineItem` tool 分支（`:2276-2291`）当前用 `className={...${item.status ?? "done"}}` 渲染——`interrupted` 会自动生成 `.interrupted` className，但**需配套写 CSS 规则**（`.manager-tool-divider.interrupted`，灰色"已中断"标签），否则与 `done` 视觉无区别。
4. `lib/types.ts` 若有镜像的 `ToolState`/timeline status 类型也同步（实施时 grep `status` 联合类型确认）。

### 10.9 【补充评审】`settleRunningTimelineItems` 的 8 个调用点必须区分语义

**核实发现**（grep 确认）：`settleRunningTimelineItems` 有 **8 个调用点**，不只 `:1986`。`interrupted` 只应作用于"用户主动停止"路径，错误路径必须保持 `error`，正常完成保持 `done`：

| 调用点 | 行号 | 当前传值 | 目标传值 | 语义 |
|---|---|---|---|---|
| response 正常完成 | `:1751` | `"done"` | `"done"`（不变） | 工具正常结束 |
| error 事件 | `:1763` | `"error"` | `"error"`（不变） | 工具报错 |
| error 事件（空消息分支） | `:1772` | `"error"` | `"error"`（不变） | 工具报错 |
| **用户主动停止** | `:1986` | `"done"` | **`"interrupted"`** | 工具被中断 |
| abort 后空消息清理 | `:2000` | `"done"` | **`"interrupted"`** | 工具被中断 |
| 真实错误 | `:2021` | `"error"` | `"error"`（不变） | 工具报错 |
| 真实错误（空消息） | `:2030` | `"error"` | `"error"`（不变） | 工具报错 |

**实施约束**：只改 `:1986` 和 `:2000` 两处为 `"interrupted"`，其余 6 处保持原值。这是 §10.4 的精确化——"停止翻 interrupted"不是全局替换，而是按调用点语义区分。

### 10.10 【补充评审】`sendChat` 的 body 消费语义

**评审点**：§10.5 的 `X-Blueprint-Session-Id` 响应头技术上可达（FastAPI `StreamingResponse` headers 在响应开始时发送，`fetch().headers.get()` 可立即读）。但 §10.2/§10.5 把 `sendChat` 描述为 fire-and-forget，若前端**完全不消费 body**，FastAPI 的 `StreamingResponse` 生成器会因无人读而触发 `GeneratorExit`——等价于客户端秒断，后端 relay 的 except 分支能否正确 catch 需验证（与 §10.1 的断连 settle 问题相关但不同：这里是"根本没开始读"而非"读到一半断"）。

**修订**：`sendChat` **不 fire-and-forget**，而是 **fetch + 读 headers + 持续读 body 但丢弃数据**：

```ts
async function sendChat(projectId, text, thinkingEffort, context, sessionId?, userMessageId?) {
  const response = await fetch(`${API_BASE}/projects/${projectId}/chat-stream`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text, session_id: sessionId ?? null,
      context, thinking_effort: thinkingEffort, message_id: userMessageId ?? null }),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await response.text() || `Chat failed: ${response.status}`);
  const resolvedSessionId = response.headers.get("X-Blueprint-Session-Id");
  // 持续读 body 但不解析——让后端 generator 正常推进到结束
  // 真正的渲染由 events SSE 驱动；这里读 body 只是为了不触发 GeneratorExit
  if (response.body) {
    const reader = response.body.getReader();
    try {
      while (true) {
        const { done } = await reader.read();
        if (done) break;
      }
    } finally {
      reader.releaseLock();
    }
  }
  return { sessionId: resolvedSessionId };
}
```

- **为什么读 body 但丢弃**：让后端 relay 生成器正常走完 lifecycle（包括最终 `upsert_message` settle），而不是被 `GeneratorExit` 打断。比"读 headers 后 abort"更稳妥——abort 仍会触发断连路径，需依赖 §10.1/§10.4 的 settle 正确性，而读到底则让正常路径完成。
- **为什么不复用 body 数据做渲染**：渲染已统一由 events SSE 驱动（§2.3.3），body 里的 `stream_event` 与 events SSE 重复；若同时消费两路会回到双份数据问题。读 body 仅为保活。
- **时序**：`response.headers` 在 fetch resolve 时就可用，前端可**立即**用 `resolvedSessionId` 订阅 events / 更新 URL，不必等 body 读完。body 读取在后台进行，与 events 订阅并行。
- **新单测 U9** `test_chat_stream_body_must_be_consumed`：后端断言——若客户端不发 abort 但停止读 body 中途，relay 仍应能完成最终 settle（验证不依赖客户端读 body 到底）。注：若 §10.1 的独立实现选择"读到底"，此测试主要防回归。

### 10.11 前端实施改动全景表（阶段 2，flag=true 路径）

> 评审提供的全景表，固化为此处的实施清单。风险标注以实际 grep 核实为准。

| 代码位置 | 当前内容 | 改动 | 风险 |
|---|---|---|---|
| `:28` | `type ToolState = "running" \| "done" \| "error"` | 加 `"interrupted"`（§10.8） | 低 |
| `:164-173` | `settleInterruptedTools` 翻 error | **整个函数删除**（§2.3.4，tools 字段不再维护） | 中——已确认唯一调用点 `:1982` |
| `:175-189` | `settleRunningTimelineItems` 签名只接受 done/error | 参数类型加 `"interrupted"`（§10.8） | 低 |
| `:1982` | `tools: settleInterruptedTools(current.tools)` | **删除这行**（tools 字段整体不再维护） | 中 |
| `:1986` | `settleRunningTimelineItems(..., "done")` | 改传 `"interrupted"`（§10.9） | 低 |
| `:2000` | `settleRunningTimelineItems(..., "done")` | 改传 `"interrupted"`（§10.9） | 低 |
| `:2276-2291` | tool 渲染用 `item.status` 作 CSS class | 加 `.interrupted` CSS 规则（灰色"已中断"标签） | 低 |
| `:588-589` | `activeAutoStreamMessagesRef` + `autoStreamSeqRef` | 删除（§2.3.3） | 低——纯清理 |
| `:591` | `remoteHydratingRef` | 删除（§2.3.5） | 低 |
| `:596-597` | `lastSavedSignatureRef` + `sessionRevisionRef` | 删除（§2.3.5） | 低 |
| `:620-657` | `saveSessionMutation` | **整段删除** | 中——已确认唯一调用点 `:910`；`api.saveChatSession`（`api.ts:332`）无其他前端调用方 |
| `:888` | `remoteHydratingRef.current = true`（hydrate effect） | 删除 guard 逻辑 | 中 |
| `:893-918` | auto-save effect（debounced PUT） | 整段删除 | 低 |
| `:955-977` | EventSource `stream_event` 分支 + auto-only gate | **删 auto-only gate**（`:956-958`），所有 session 都处理 `stream_event` | 中——核心逻辑变化；需确认非 auto session 的 `applyStreamEvent` 收到事件时不覆盖本地乐观状态（靠 id 对齐，§10.2） |
| `:966` | `remoteHydratingRef.current = true`（events effect） | 删除（ref 已删） | 低 |
| `:997` | `lastSavedSignatureRef.current = ...`（message_upsert） | 删除（ref 已删） | 低 |
| `:1901-1914` | `submit()` 本地组装 user + manager 消息 | 改为**乐观追加 user**（§10.2）+ 不组装 manager | 高——核心用户交互路径 |
| `:1960-1970` | `api.streamChat(...)` + reader 回调 | 改为 `api.sendChat(...)`（§10.10，读 body 保活但不渲染） | 高 |
| `:1990-2036` | abort/catch/finally 错误处理 | 保留结构，删 `tools` 相关，`:1986/:2000` 改 interrupted | 高 |

**阶段 3 收尾**：`:1224-1243` `finalizeCompaction` 简化为只订阅；`api.ts:344-354` `appendChatSessionMessages` 删除（已确认面板未用）；`api.ts:488-555` `streamChat` reader → `sendChat`（§10.10）。

**阶段 4 清理**：删除 feature flag + 旧 fetch 路径死代码。

### 10.12 最大整体风险：submit() 三线交汇

评审结论"submit() 路径改造是最大风险"完全成立——它同时涉及 §10.2（乐观渲染）、§10.5（session ID 回填）、§10.3（flag 切换），三条线在同一个函数里交汇。

**实施约束**：`submit()` 的新旧路径用 flag **严格分支**，确保 flag=false 时零行为变化：

```ts
async function submit() {
  // ... 公共前置（draft 校验、/compact 短路）...
  if (!singleSourceEnabled) {
    return submitLegacy();  // 旧路径：本地组装 + streamChat reader + auto-save
  }
  return submitSingleSource();  // 新路径：乐观 user + sendChat + events 驱动
}
```

`singleSourceEnabled` 来自 §10.3 的 config 端点。两条路径并存于代码，阶段 4 删除 legacy 分支。这样 flag=false 时 `submit()` 完全走旧逻辑，回归风险为零；flag=true 时走新逻辑，出问题翻 flag 即可。

---

## 11. 分阶段测试验收规范（LLM 实施可执行版）

> 本节把 §5 的单测清单和 §4 的手测清单细化为**逐阶段、可逐条执行**的规格。每条测试写出：文件路径、fixture/setup、操作序列、断言条件、预期输出。实施 LLM 不需要理解架构，按本节操作即可。

### 11.1 测试基建：共享 fixture 约定

> 当前 `conftest.py` 无共享 fixture，每个测试文件自管理。本次新增的测试文件遵循已有 `unittest.TestCase` + 临时目录隔离模式（参考 `test_auto_command_interception.py` / `test_subgraph_run.py`）。

**新文件**：`backend/tests/test_chat_stream_relay.py`

```python
"""Shared constants and helpers for chat stream relay tests."""

# ── 确定性事件 fixture ──
# 包含所有事件类型的代表性序列（thinking → text → tool → response → done）
# 用于 U1 / U2 / U6 等需要可复现输入的测试

FIXED_NOW = 1_700_000_000_000  # 毫秒时间戳，2023-11-14T22:13:20Z

EVENT_FIXTURE_PAYLOADS = [
    {"type": "thinking_start", "assistant_turn_index": 0, "content_index": 0},
    {"type": "thinking_delta", "delta": "Let me analyze this.",
     "assistant_turn_index": 0, "content_index": 0},
    {"type": "thinking_end", "content": "Let me analyze this.",
     "assistant_turn_index": 0, "content_index": 0},
    {"type": "text_delta", "delta": "Based on my analysis,",
     "assistant_turn_index": 0, "content_index": 1},
    {"type": "text_delta", "delta": " the answer is 42.",
     "assistant_turn_index": 0, "content_index": 1},
    {"type": "tool_start", "tool_call_id": "call_abc",
     "tool_name": "read_file", "label": "Reading config.json"},
    {"type": "tool_end", "tool_call_id": "call_abc",
     "tool_name": "read_file", "is_error": False},
    {"type": "tool_report", "tool_call_id": "call_abc",
     "tool_name": "read_file", "summary": "File read: config.json (42 lines)"},
    {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 50,
     "total_tokens": 150}},
    {"type": "response", "response": {
     "message": "Based on my analysis, the answer is 42.",
     "thinking": "Let me analyze this.",
     "metadata": {"token_usage": {"input_tokens": 100, "output_tokens": 50,
       "total_tokens": 150}}}},
    {"type": "done"},
]

# ── 事件序列转 SSE 字节 ──
def payloads_to_sse_bytes(payloads: list[dict]) -> list[bytes]:
    """将事件 fixture 转为 manager_service.stream_chat 返回的 SSE 字节格式。"""
    return [
        f'data: {json.dumps(p)}\n\n'.encode() for p in payloads
    ]

# ── 时间补丁 ──
# 使用 patch.object 同时冻结两个时钟，确保两路径产出相同时间戳
# patch.object(ChatStreamRelay, '_now_ms', return_value=FIXED_NOW)
# patch('time.monotonic', return_value=0.0)  # 让所有事件都在 0.75s 窗口内

# ── Mock ChatSessionService ──
class RecordingChatSessionService:
    """记录所有 upsert_message / publish_stream_event 调用，用于比较两路径产出。"""
    def __init__(self):
        self.upsert_calls: list[tuple[str, str, ChatSessionMessage]] = []
        self.published_events: list[dict] = []
        self._messages: dict[tuple[str, str], list[ChatSessionMessage]] = {}

    def upsert_message(self, project_id, session_id, message):
        key = (project_id, session_id)
        msgs = self._messages.setdefault(key, [])
        for i, m in enumerate(msgs):
            if m.id == message.id:
                msgs[i] = message
                break
        else:
            msgs.append(message)
        self.upsert_calls.append((project_id, session_id, message.model_copy(deep=True)))

    def publish_stream_event(self, project_id, session_id, *, message_id, event, seq=None, revision=None):
        self.published_events.append({
            "project_id": project_id, "session_id": session_id,
            "message_id": message_id, "event": event, "seq": seq,
        })

    def get_session(self, project_id, session_id):
        # 返回一个含已 upsert 消息的 fake session（用于 settle_message 读取）
        key = (project_id, session_id)
        msgs = self._messages.get(key, [])
        return ChatSession(session_id=session_id, summary="", messages=list(msgs),
                          created_at=FIXED_NOW, updated_at=FIXED_NOW, revision=len(msgs))
```

### 11.2 阶段 0 测试（go/no-go 门禁）

#### U1: 双出口等价（门禁测试）

**文件**：`backend/tests/test_chat_stream_relay.py`
**类名**：`ChatStreamRelayDualOutputTest`

```
测试名：test_stream_to_http_matches_run_to_session

Setup:
  1. 创建 RecordingChatSessionService（recording_run）
  2. 创建 RecordingChatSessionService（recording_http）
  3. 创建 mock ManagerService，stream_chat 返回 payloads_to_sse_bytes(EVENT_FIXTURE_PAYLOADS)
  4. 创建两个 ChatStreamRelay 实例（各用对应的 recording service）
  5. patch ChatStreamRelay._now_ms 返回 FIXED_NOW
  6. patch time.monotonic 返回 0.0（让所有事件都在 persist 窗口内）

操作 A（run_to_session 路径）：
  relay_run.run_to_session("proj", "sess", ChatRequest(message="hi"),
                           message_id="mgr_test")

操作 B（stream_to_http 路径）：
  list(relay_http.stream_to_http("proj", "sess", ChatRequest(message="hi"),
                                  message_id="mgr_test"))
  # 消费生成器到结束

断言：
  1. 取 recording_run.upsert_calls 最后一条的 message（称为 msg_run）
  2. 取 recording_http.upsert_calls 最后一条的 message（称为 msg_http）
  3. assert msg_run.id == msg_http.id == "mgr_test"
  4. assert msg_run.state == msg_http.state == "done"
  5. assert msg_run.content == msg_http.content
     == "Based on my analysis, the answer is 42."
  6. assert msg_run.thinking == msg_http.thinking == "Let me analyze this."
  7. assert len(msg_run.timeline) == len(msg_http.timeline)
     # 逐条比对 timeline item
  8. for item_run, item_http in zip(msg_run.timeline, msg_http.timeline):
       assert item_run.id == item_http.id
       assert item_run.kind == item_http.kind
       assert item_run.status == item_http.status
       assert item_run.content == item_http.content
       assert item_run.tool_name == item_http.tool_name
  9. assert msg_run.token_usage.total_tokens == msg_http.token_usage.total_tokens
  10. # 验证 stream_to_http yield 的字节数 > 0
      assert len(http_bytes) > 0

预期：
  - 两路径最终 message 逐字段相等
  - 如果任何字段不等 → 方案有未发现的分叉，需重新审视
```

**为什么这是门禁**：U1 如果失败，说明 `_apply_stream_payload` 在两路径下的调用方式有差异（如 persist 时机不同导致 settle 行为不同），或 `stream_to_http` 遗漏了某个 `_apply_stream_payload` 调用。此时不应继续阶段 1。

#### U1b: 异常路径等价

**文件**：`backend/tests/test_chat_stream_relay.py`
**测试名**：`test_stream_to_http_error_path_matches_run_to_session`

```
Setup:
  与 U1 相同，但事件 fixture 替换为含 error 事件的序列：
  ERROR_FIXTURE = [
    {"type": "thinking_start"},
    {"type": "text_delta", "delta": "Partial response"},
    {"type": "error", "detail": "Upstream timeout"},
  ]

操作 A：relay_run.run_to_session(...)
  预期：抛出 RuntimeError

操作 B：消费 relay_http.stream_to_http(...)
  预期：生成器正常结束（yield error SSE 字节后停止），不抛异常

断言：
  1. msg_run.state == "error"
  2. msg_http.state == "error"（从最后一条 upsert 取）
  3. msg_run.content == msg_http.content
  4. # 验证 error 事件被 yield 为 SSE 字节
     last_bytes = [b for b in http_bytes if b"error" in b]
     assert len(last_bytes) > 0

预期：
  两路径的 error settle 语义一致：message.state == "error"，timeline running 项被 settle
```

### 11.3 阶段 1 测试

#### U2: `/chat-stream` 写入用户消息

**文件**：`backend/tests/test_chat_stream_relay.py`
**类名**：`ChatStreamEndpointTest`（使用 `TestClient(app)`）

```
测试名：test_chat_stream_writes_user_message

Setup:
  1. 创建临时目录 + override get_settings().data_root
  2. 创建真实 ProjectService + ProjectEventService + ProjectService 创建项目
  3. 通过 API 创建 chat session：POST /api/projects/{id}/chat-sessions
  4. patch ManagerService.stream_chat 返回 EVENT_FIXTURE_PAYLOADS 的 SSE 字节
  5. patch get_settings().chat_single_source = True（启用 flag）

操作：
  POST /api/projects/{id}/chat-stream
  body: {"message": "帮我看看这个项目", "session_id": "<created_session_id>"}

断言：
  1. response.status_code == 200
  2. response.headers["X-Blueprint-Session-Id"] == <created_session_id>
  3. GET /api/projects/{id}/chat-sessions/<session_id>
     session.messages 长度 == 2
  4. session.messages[0].role == "user"
     session.messages[0].content == "帮我看看这个项目"
  5. session.messages[1].role == "manager"
     session.messages[1].state == "done"
  6. session.messages[1].timeline 长度 > 0
  7. session.revision > 0（至少一次 upsert）

Cleanup：
  get_settings.cache_clear() 及所有 lru_cache 工厂
  shutil.rmtree(tempdir)
```

#### U3: 前端不再需要 PUT

```
测试名：test_chat_stream_no_frontend_put_needed

操作：
  1. 同 U2 setup，发一条消息
  2. 不调 PUT /api/projects/{id}/chat-sessions/{session_id}
  3. 直接 GET 验证

断言：
  同 U2 断言 3-7（消息完整持久化，不依赖前端回写）
```

#### U4: 旁路写入不被覆盖

```
测试名：test_chat_stream_background_write_not_overwritten

Setup：
  1. 创建 session + 写一条 user 消息（通过 append_messages）
  2. patch stream_chat 为慢速生成器（yield 一个 text_delta 后 sleep 0.5s 再 yield done）

操作（并发）：
  线程 A：POST /chat-stream（触发 relay，正在处理中）
  线程 B：在 relay 处理过程中，直接调用 chat_session_service.append_messages
          写入一条 depjob_terminal 消息

断言：
  relay 完成后 GET session：
  1. session.messages 包含 3 条消息：user + manager + depjob_terminal
  2. depjob_terminal 消息未被 relay 的 upsert 覆盖
     （因为 relay 用 upsert_message 按 id 更新，不会整体替换 messages 列表）
```

#### U5: events SSE 携带 revision 和 seq

```
测试名：test_events_sse_carries_revision_and_seq

Setup：
  1. chat_session_service.subscribe_events("proj", "sess") 开一个订阅者线程
  2. patch stream_chat 返回 EVENT_FIXTURE_PAYLOADS

操作：
  relay.run_to_session(...)

断言（从订阅者队列读取事件）：
  1. 收到 message_upsert 事件，payload["revision"] 为整数且单调递增
  2. 收到 stream_event 事件，payload["seq"] 为整数且单调递增
  3. 最后一条 message_upsert 的 revision > 1
  4. stream_event 的 seq 序列从 1 开始连续递增
```

#### U8: 响应头 `X-Blueprint-Session-Id`

```
测试名：test_chat_stream_returns_session_id_header

Setup：
  同 U2，但 session_id 为空（触发 _ensure_session 创建新会话）

操作：
  POST /api/projects/{id}/chat-stream
  body: {"message": "hi"}  # 不带 session_id

断言：
  1. response.status_code == 200
  2. "X-Blueprint-Session-Id" in response.headers
  3. session_id = response.headers["X-Blueprint-Session-Id"]
  4. session_id 非空且以 "session_" 开头
  5. GET /api/projects/{id}/chat-sessions/{session_id} 返回 200
```

#### U9: body 必须被消费但客户端秒断不崩溃

```
测试名：test_chat_stream_client_disconnect_settles_message

Setup：
  1. patch stream_chat 为慢速生成器（每秒 yield 一个 text_delta）
  2. 创建 TestClient

操作：
  1. POST /chat-stream，读 2 个 chunk 后关闭连接（模拟客户端秒断）
  3. 等待 relay 线程完成（最多 5s）

断言：
  1. GET session，manager 消息 state == "done" 或 "error"（取决于实现选择）
  2. 不是 "thinking" 或 "streaming"（不能残留运行中状态）
  3. timeline 中无 status == "running" 的项
```

### 11.4 阶段 2 测试（前端）

#### F1: 编译通过

```
命令：cd frontend && npm run build

预期：
  - 退出码 0
  - 无 TypeScript 编译错误
  - 特别检查：ToolState 类型扩展后，所有引用点清理干净
  - grep "settleInterruptedTools" frontend/components/ → 无结果（函数已删除）
  - grep "saveSessionMutation" frontend/components/ → 无结果
  - grep "activeAutoStreamMessagesRef" frontend/components/ → 无结果
```

#### F2: 瘦 reducer 跨语言一致性（可选但推荐）

```
文件：backend/tests/test_chat_stream_relay.py
测试名：test_apply_stream_payload_fixture_matches_ts_reducer

目的：
  用 EVENT_FIXTURE_PAYLOADS 喂 Python _apply_stream_payload，
  与 TypeScript applyStreamEvent 的已知输出对比。

做法：
  1. Python 端：逐条应用 EVENT_FIXTURE_PAYLOADS 到 _apply_stream_payload
     记录每步的 message snapshot
  2. 将最终 message 导出为 JSON（含 timeline、content、thinking、state）
  3. 前端：写一个临时脚本，用相同的 EVENT_FIXTURE_PAYLOADS 跑
     applyStreamEvent 并 JSON.stringify 输出
  4. 比较两份 JSON

预期：
  - timeline 项的 id、kind、status、content 完全一致
  - message.content、message.thinking、message.state 一致

注：
  这是跨语言对拍，不是自动化单测。实施时可以用 Node.js 子进程跑
  TypeScript fixture，将 stdout JSON 与 Python 端比较。
```

#### F3: flag=false 时零行为回归

```
验证方式：手动 + 构建

操作：
  1. cd frontend && npm run build（flag=false 构建）
  2. 启动后端 BLUEPRINT_CHAT_SINGLE_SOURCE=false
  3. 走 §4 手测清单的 M1（手动发一条消息）

断言：
  - 前端仍走旧 fetch 直读路径（`submitLegacy`）
  - 消息正常出现，timeline 正常渲染
  - 无新增 console error
```

### 11.5 阶段 3 测试（flag=true 灰度）

#### U7: 停止时 interrupted 语义

```
测试名：test_chat_stream_abort_settles_interrupted_not_error

Setup：
  1. patch stream_chat 为含 tool_start 但无 tool_end 的序列：
     [{"type": "thinking_start"},
      {"type": "text_delta", "delta": "Working..."},
      {"type": "tool_start", "tool_call_id": "call_xyz",
       "tool_name": "run_tests", "label": "Running tests"},
      {"type": "error", "detail": "Client disconnected"}]
  2. 使用 RecordingChatSessionService

操作：
  relay.run_to_session("proj", "sess", ChatRequest(message="run tests"),
                       message_id="mgr_test")
  预期：抛出 RuntimeError

断言：
  1. 最后 upsert 的 message.state == "error"
  2. timeline 中 tool_call_id=="call_xyz" 的项 status == "error"
     （因为 error 事件触发 settle_stream_message("error")）
  3. # 注意：此测试验证后端 relay 的 error 路径
  4. # interrupted 语义在前端实现（§10.4/§10.9）
     # 后端 relay 断连时 settle 为 "error"，前端停止按钮路径
     # 由前端 settleRunningTimelineItems("interrupted") 处理

补充测试：test_frontend_stop_sets_interrupted_status（§10.4 前端验证）
  验证 ManagerChatPanel.tsx:1986 的 settleRunningTimelineItems
  传参为 "interrupted" 而非 "done"
  （实施时 grep 确认：grep -n '"interrupted"' ManagerChatPanel.tsx）
```

#### U10: 斜杠命令 fanout events

```
测试名：test_slash_command_publishes_message_upsert

Setup：
  1. 创建 session + 订阅 events
  2. POST /chat-stream body: {"message": "/auto status"}

操作：
  消费 SSE 响应 + 从 events 订阅者读取事件

断言：
  1. SSE 响应含 text_delta + done 事件
  2. events 订阅者收到 message_upsert 事件（cmd_usr_* + cmd_mgr_* 消息）
  3. GET session 含两条消息
```

### 11.6 每阶段验收门禁汇总

| 阶段 | 必须通过的测试 | 门禁级别 | 失败处理 |
|---|---|---|---|
| **0** | U1 + U1b + 现有全量单测绿 | **硬门禁**：U1 失败 = 方案重新审视 | 停止，不进入阶段 1 |
| **1** | U2 + U3 + U4 + U5 + U8 + U9 + 现有全量绿 | 硬门禁 | 修复后重跑 |
| **2** | F1（build 通过）+ F3（flag=false 回归）+ U1 仍绿 | 硬门禁 | 修复后重跑 |
| **3** | U7 + U10 + F2（可选）+ §4 手测 M1-M4, S1-S3, T1-T3 | 手测门禁 | 灰度期修复 |
| **4** | 全量单测 + §4 全量手测 + F1 | 清理门禁 | 回归修复 |

### 11.7 测试文件清单与命名

```
backend/tests/
├── test_chat_stream_relay.py      # U1, U1b（阶段 0）
├── test_chat_stream_endpoint.py   # U2, U3, U4, U8, U9（阶段 1）
├── test_chat_events_sse.py        # U5, U10（阶段 1+3）
├── test_chat_interrupted.py       # U7（阶段 3）
└── ...existing tests...           # 不动

frontend/
├── 无新增自动化测试（项目无前端测试基建）
└── §4 手测清单作为人工验收标准
```

### 11.8 U1 fixture 的预期输出（参考基线）

> 实施 U1 时可用此作为"正确答案"验证测试本身是否正确。以下是 EVENT_FIXTURE_PAYLOADS 喂给 `_apply_stream_payload` 后的**预期最终 message**（假设 `_now_ms()` 返回 `FIXED_NOW`）：

```python
expected_final_message = ChatSessionMessage(
    id="mgr_test",
    role="manager",
    content="Based on my analysis, the answer is 42.",
    thinking="Let me analyze this.",
    state="done",
    token_usage=ChatTokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
    timeline=[
        # thinking item（thinking_start 创建，thinking_end 关闭）
        ChatSessionMessageTimelineItem(
            id="thinking_0_0",
            kind="thinking",
            content="Let me analyze this.",
            status="done",
            started_at=FIXED_NOW,
            ended_at=FIXED_NOW,
        ),
        # text item（text_delta 累积，response 时 settle）
        ChatSessionMessageTimelineItem(
            id="text_0_1",
            kind="text",
            content="Based on my analysis, the answer is 42.",
            status="done",
        ),
        # tool item（tool_start → tool_end → tool_report）
        ChatSessionMessageTimelineItem(
            id="call_abc",
            kind="tool",
            label="Reading config.json",
            tool_name="read_file",
            content="File read: config.json (42 lines)",
            status="done",
            started_at=FIXED_NOW,
            ended_at=FIXED_NOW,
        ),
        # response 事件触发 _finalize_response_timeline，
        # 可能插入 thinking_final / text_final（如果对应 content 尚无 timeline 项）
        # 此处 thinking 和 text 已有对应 timeline 项，所以不额外插入
    ],
)
```

**验证步骤**（实施时先跑此基线确认 fixture 正确）：
1. 创建 `ChatStreamRelay(mock_chat_svc, mock_mgr_svc)`
2. 逐条 `EVENT_FIXTURE_PAYLOADS` 调用 `_apply_stream_payload`
3. 比对输出与 `expected_final_message`
4. 如果任何字段不匹配 → fixture 或预期值需要调整

### 11.9 前端手测执行脚本（人工验收用）

> §4 的手测清单需要配合以下环境准备：

```bash
# 阶段 3 手测前准备
# 1. 后端启用 flag
echo "BLUEPRINT_CHAT_SINGLE_SOURCE=true" >> ~/.config/blueprint-re/backend.env
systemctl --user restart blueprint-re-backend.service

# 2. 前端构建（含 flag 分支）
cd frontend && npm run build
systemctl --user restart blueprint-re-frontend.service

# 3. 验证服务健康
curl -fsS http://127.0.0.1:18001/healthz
curl -I http://127.0.0.1:13001

# 4. 浏览器打开 http://127.0.0.1:13001
# 5. 按 §4 清单逐条执行，在每条 [] 打 ✓ 或 ✗

# 阶段 3 手测后回滚（如果出问题）
sed -i 's/BLUEPRINT_CHAT_SINGLE_SOURCE=true/BLUEPRINT_CHAT_SINGLE_SOURCE=false/' \
  ~/.config/blueprint-re/backend.env
systemctl --user restart blueprint-re-backend.service
```

### 11.10 测试覆盖率边界声明

以下场景**不在**自动化单测范围内，依赖 §4 手测：

| 场景 | 原因 | 手测编号 |
|---|---|---|
| 流式中刷新页面（F5） | 需要真实浏览器 + SSE 重连 | M2 |
| 多标签页并发 | 需要多浏览器上下文 | X1, X2 |
| 工具调用真实执行 | 需要 pi executor 环境 | T1, T2, T3 |
| auto/wake 后台触发 | 需要完整 workboard 生命周期 | A1, A2, A3 |
| 附件上传 | 需要文件系统 + upload API | M4 |
| 网络断连重连 | 需要网络层干预 | S2 |
