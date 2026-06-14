import {
  Agent,
  DEFAULT_COMPACTION_SETTINGS,
  compact,
  estimateContextTokens,
  prepareCompaction,
} from "@earendil-works/pi-agent-core";
import { clampThinkingLevel, getModel, registerBuiltInApiProviders, Type } from "@earendil-works/pi-ai";
import { buildSessionContext } from "../node_modules/@earendil-works/pi-agent-core/dist/harness/session/session.js";

registerBuiltInApiProviders();

const HOST = process.env.MANAGER_AGENT_HOST || "127.0.0.1";
const PORT = Number(process.env.MANAGER_AGENT_PORT || "18002");
const PROVIDER = process.env.MANAGER_AGENT_PROVIDER || "deepseek";
const MODEL = process.env.MANAGER_AGENT_MODEL || process.env.BLUEPRINT_MANAGER_MODEL || "deepseek-v4-pro";
const API_KEY = process.env.MANAGER_AGENT_API_KEY || process.env.BLUEPRINT_DEEPSEEK_API_KEY || "";
const TIMEOUT_MS = Number(process.env.MANAGER_AGENT_TIMEOUT_MS || "600000");
const HEARTBEAT_INTERVAL_MS = 5000;
const WAIT_LOG_INTERVAL_MS = Number(process.env.MANAGER_AGENT_WAIT_LOG_INTERVAL_MS || "30000");
const MANAGER_WEBSEARCH_ENABLED = /^(1|true|yes|on)$/i.test(process.env.MANAGER_WEBSEARCH_ENABLED || "");
const TAVILY_API_KEY = process.env.TAVILY_API_KEY || "";
const TAVILY_BASE_URL = process.env.TAVILY_BASE_URL || "https://api.tavily.com";
const MANAGER_CONTEXT_WINDOW_TOKENS = Number(process.env.MANAGER_CONTEXT_WINDOW_TOKENS || "1000000");
const MANAGER_COMPACTION_ENABLED = !/^(0|false|no|off)$/i.test(process.env.MANAGER_COMPACTION_ENABLED || "true");
const MANAGER_COMPACTION_KEEP_RECENT_TOKENS = Number(process.env.MANAGER_COMPACTION_KEEP_RECENT_TOKENS || "120000");
const MANAGER_COMPACTION_RESERVE_TOKENS = Number(process.env.MANAGER_COMPACTION_RESERVE_TOKENS || "16000");
const PROVIDER_MAX_RETRIES = Number(process.env.MANAGER_AGENT_PROVIDER_MAX_RETRIES || "5");
const PROVIDER_MAX_RETRY_DELAY_MS = Number(process.env.MANAGER_AGENT_PROVIDER_MAX_RETRY_DELAY_MS || "16000");

function resolveManagerConfig(payload = {}) {
  const config = payload.manager_config && typeof payload.manager_config === "object" ? payload.manager_config : {};
  return {
    provider: config.provider || PROVIDER,
    model: config.model || MODEL,
    apiKey: config.api_key || API_KEY,
    deepseekApiBaseUrl: config.deepseek_api_base_url || process.env.BLUEPRINT_DEEPSEEK_API_BASE_URL || "",
    piDeepseekBaseUrl: config.pi_deepseek_base_url || process.env.BLUEPRINT_PI_DEEPSEEK_BASE_URL || "",
    websearchEnabled:
      typeof config.websearch_enabled === "boolean" ? config.websearch_enabled : MANAGER_WEBSEARCH_ENABLED,
    tavilyApiKey: config.tavily_api_key || TAVILY_API_KEY,
    tavilyBaseUrl: config.tavily_base_url || TAVILY_BASE_URL,
    providerProtocol: config.provider_protocol || null,
    selectedProviderId: config.selected_provider_id || null,
  };
}

function normalizeBaseUrl(value) {
  return typeof value === "string" ? value.trim().replace(/\/+$/, "") : "";
}

function resolveModel(runtimeConfig) {
  // 1. Try registry first
  let model = getModel(runtimeConfig.provider, runtimeConfig.model);
  if (model) {
    if (runtimeConfig.provider === "deepseek") {
      const deepseekBaseUrl = normalizeBaseUrl(runtimeConfig.piDeepseekBaseUrl);
      if (deepseekBaseUrl) return { ...model, baseUrl: deepseekBaseUrl };
    }
    return model;
  }
  // 2. Registry miss — build from protocol
  const protocol = runtimeConfig.providerProtocol;
  if (protocol === "anthropic_compatible") {
    return {
      id: runtimeConfig.model,
      provider: runtimeConfig.selectedProviderId || runtimeConfig.provider,
      name: runtimeConfig.model,
      api: "anthropic-messages",
      baseUrl: normalizeBaseUrl(runtimeConfig.deepseekApiBaseUrl) || "https://api.anthropic.com",
      contextWindow: 200000,
    };
  }
  if (protocol === "openai_compatible") {
    return {
      id: runtimeConfig.model,
      provider: runtimeConfig.selectedProviderId || runtimeConfig.provider,
      name: runtimeConfig.model,
      api: "openai-completions",
      baseUrl: normalizeBaseUrl(runtimeConfig.deepseekApiBaseUrl) || "https://api.openai.com/v1",
      contextWindow: 128000,
    };
  }
  return null;
}

let startupValidation = null;

function validateStartupConfig() {
  if (startupValidation) {
    return startupValidation;
  }
  const errors = [];
  const runtimeConfig = resolveManagerConfig();
  // API credentials may come from backend-supplied manager_config on each request,
  // so startup readiness must not require a fallback env key.
  const model = resolveModel(runtimeConfig);
  if (!model) {
    errors.push(`Manager model not found: provider=${runtimeConfig.provider}, model=${runtimeConfig.model}`);
  }
  const deepseekBaseUrl = normalizeBaseUrl(runtimeConfig.piDeepseekBaseUrl);
  if (runtimeConfig.provider === "deepseek" && deepseekBaseUrl) {
    try {
      new URL(deepseekBaseUrl);
    } catch {
      errors.push(`Invalid DeepSeek base URL: ${deepseekBaseUrl}`);
    }
  }
  startupValidation = {
    ok: errors.length === 0,
    errors,
    provider: runtimeConfig.provider,
    model: runtimeConfig.model,
  };
  return startupValidation;
}

function buildSystemPrompt(runtimeConfig = resolveManagerConfig()) {
  const webCapabilityLines =
    runtimeConfig.websearchEnabled && runtimeConfig.tavilyApiKey
      ? [
          "- web_search finds current public web information when up-to-date external context is required.",
          "- web_extract reads the content of a specific public web page after search identifies a source.",
        ]
      : [];
  const webJudgmentLines =
    runtimeConfig.websearchEnabled && runtimeConfig.tavilyApiKey
      ? [
          "- Use web_search or web_extract when the user explicitly asks for current/latest information, when external docs or recent package behavior matters, or when you need to verify a claim before editing the blueprint.",
          "- Do not use web tools when project context is already sufficient, or when doing so could expose local secrets or private project content.",
        ]
      : [];
  return `You are the Manager AI for Blueprint RE, a bioinformatics workflow manager.

You are an interactive project agent. Answer directly when the user is asking a general question. Use tools when they materially improve correctness or when the user asks you to inspect or change the current project.

Core model:
- The blueprint is represented by cards. A card is the editable unit of the blueprint.
- Data assets are referenced by asset_id. Card outputs are expected/planned assets; later card inputs can reuse those asset_ids.
- Card step is the timeline layer. A card must be later than the assets it consumes.
- Card step is the parallel-execution layer, not a serial sequence number. Two cards that share no dependency relationship (same min-step floor and no transitive input/output link) MUST share the same step value. Only increment step when a card consumes an asset produced by an earlier step.
- When creating or revising multiple cards in one turn, align cards at the smallest valid step. Use get_background_workboard.ready_to_start[].parallel_group as the authoritative parallel layer for existing planned cards; new cards that belong to the same layer should reuse that step value.
- A workboard parallel_group like "step_N" means the backend has already verified these cards can run together. Do not reassign them to different steps without a dependency reason.
- The DAG is already visible in the UI. Do not narrate the full graph back to the user unless they explicitly ask for a graph recap.

Available capabilities:
- inspect_project_summary reads a compact project summary with card ids/titles/status/steps, active runs, blockers, and asset counts.
- get_background_workboard reads the backend-derived background workboard. Use it in auto/background turns instead of reconstructing status from general inspect tools.
- inspect_dependency_attention reads derived dependency ATTENTION diagnostics. Use it after revise_card_plan/delete_card returns dependency_attention_check_recommended, or when summary/detail reports dependency_attention.
- find_cards searches cards by query, status, step, or asset_id.
- find_assets searches materialized and planned assets by role, artifact_class, format, producer card, status, or query.
- get_card_detail reads one card body, executor_context, inputs, outputs, and recent runs.
- get_asset_detail reads one asset detail when file preview, script binding, or manifest-level diagnosis is needed.
- get_project_context reads the full project context. Use it only when compact inspect/find/detail tools are insufficient.
- list_data_assets reads the full data asset timeline. Use it only when compact inspect/find/detail tools are insufficient.
- list_project_memory reads short-lived-to-long-term project preferences and corrections. It is not the source of project execution facts.
- write_project_memory stores only explicit user preferences and corrections, such as "remember this", "default to this", or "do not do this again".
- create_card, revise_card_plan, annotate_card, and delete_card directly modify blueprint cards after backend validation.
- configure_card_execution directly updates selected skills, MCP servers, and Python/R runtime bindings for one or more cards.
- install_runtime_dependencies starts a background job that installs explicitly named Python/R packages into an already selected non-system runtime. When fixing a specific card/run dependency issue, include source.card_id and source.run_id when known. Treat it like card execution background work: after a successful start, report the job_id and stop; do not foreground-poll the job in the same turn. The current default policy is allow_safe_registry_install: when the resolver approves every package as a single-family request, pip / cran / bioconductor actions may execute through structured backend commands. For current R dual-source fallback hints, the resolver prefers CRAN. Never invent or pass a package manager yourself.
- resolve_runtime_dependencies returns a resolver plan (status, installable, blocked, fallback_actions) for the same request shape as install_runtime_dependencies, without creating a background job. Use it to inspect what would happen before mutating runtime state. The plan never executes a partial install: if status is not "fully_installable", the caller must not retry install_runtime_dependencies with the same payload. If status is "partial_resolution_requires_manual_preparation" and the user has approved a narrower install, submit a new explicit request that lists only the installable subset.
- get_runtime_dependency_install_status checks whether a previously started dependency installation job has finished. Use it for explicit user checks, recovery, or a later wake turn; do not use it to poll a just-started job in the same turn.
- promote_workboard_item_to_todo, claim_workboard_item, complete_workboard_item, defer_workboard_item, block_workboard_item_for_user, reopen_workboard_item, skip_workboard_item, and submit_claimed_workboard_items consume backend workboard items. Use them in auto/background turns and also for user-driven "continue / resume / run next / run ready cards / run several cards" requests; do not invent your own todo list.
- finish_auto_episode confirms the current auto episode is complete and transitions it to finished. Valid only when the auto state is "complete". After calling this, the episode terminates and no further auto wakes occur.
- start_card_run, stop_card_run, rerun_card, and review_card_run control card execution directly when execution should happen now. Reserve start_card_run for one explicit card. For frontier or batch execution, prefer get_background_workboard -> promote/claim/submit. After a successful start_card_run or rerun_card launch, do not poll card status in the same turn. Briefly report the run_id and stop so run events/wake events can carry progress.
- cleanup_run_history removes old finished run execution files/caches when they are no longer needed; by default it preserves runs that own valid accepted assets.
- search_card_templates, save_card_template, and instantiate_card_template manage reusable manager-only card templates.
- read_result_asset reads a whitelisted result asset preview by asset_id.
- list_skill_library and list_mcp_library browse id/name-only inventories.
- search_skill_library and search_mcp_library return id/name-only matches.
- get_skill_library_item and get_mcp_library_item read one registry entry in more detail only when the name is not enough.
${webCapabilityLines.join("\n")}

Judgment:
- Decide whether current project context is needed. If exact card ids, asset ids, steps, or current blueprint state matter, use inspect_project_summary or find_* first.
- In auto/background turns, call get_background_workboard first. Each turn should end with at most one async-boundary-yielding action batch: one submit_claimed_workboard_items call, or one start_card_run / rerun_card, or one install_runtime_dependencies. You may combine independent non-yielding tool calls (inspect_*, find_*, get_*, configure_card_execution, annotate_card, write_project_memory) freely. You may also start one install_runtime_dependencies plus one run-yielding action in the same turn when they are truly independent; the async boundary will still yield the turn once. Do not call start_card_run repeatedly in the same turn to emulate a batch — use submit_claimed_workboard_items instead. The workboard response includes available_slots — never submit a batch larger than available_slots. Excess items will be automatically deferred and re-woken when capacity frees up.
- In auto mode, an empty ready_to_start/todo frontier means "no backend-startable work right now", not "the user's objective is done." For new projects or uploaded-data exploration, inspect the project/assets and create a small exploration card if no suitable card exists. When a completed exploration card produces useful outputs but no downstream card exists, either create the next planned card from those outputs or finish only after summarizing why no further card is needed.
- If the user asks to continue, resume, start pending work, run ready cards, run the next step, or run several cards, call get_background_workboard first even outside auto mode.
- For broad workflow additions, use inspect_project_summary and find_assets before choosing steps and asset_ids.
- For plotting style, report style, recurring user preferences, or previously corrected behavior, read project memory when relevant.
- Treat the blueprint/cards/assets/runs as the source of project execution facts. Do not write blueprint facts into project memory.
- Treat skills and MCP servers as optional ids for card execution, not as always-on built-in powers. Use list/search only when a card clearly benefits from reusable abilities, and prefer attaching by obvious id/name without reading details.
- For simple conceptual questions, answer without tools.
- For blueprint/card changes, use find_cards/get_card_detail for existing cards and find_assets for inputs. Use card write tools directly once you have enough context. Do not describe a change as complete unless a write tool succeeded.
- After start_card_run or rerun_card returns background/async_boundary/do_not_poll, do not call get_card_detail, find_assets, inspect_project_summary, or cleanup tools just to wait for that run. End the turn with the run_id unless the tool returned ok:false or pending approvals.
- After install_runtime_dependencies returns background/job_id, do not call get_runtime_dependency_install_status, inspect_project_summary, get_card_detail, find_assets, or cleanup tools just to wait for that job. End the turn with the job_id and wait for project-state events or runtime dependency wake events.
- If install_runtime_dependencies returns a duplicate error (duplicate_dependency_resolution_in_progress or duplicate_dependency_resolution_failure), do not retry the same package/runtime combination. Treat in-flight duplicates as wait states and terminal duplicates as non-retryable failures.
- If install_runtime_dependencies returns a non-background blocked status (status starting with "partial_resolution_", "fallback_available_", "manual_preparation_required", "unsupported_source_spec", "runtime_missing", "solver_error", or "resolution_unknown"), do not transform it into a shell/install workaround. Summarize the resolver_plan.installable and resolver_plan.blocked sections, tell the user exactly what blocked which package, and ask whether they want to (a) manually prepare the runtime or (b) submit a narrower explicit request for only the installable subset. Never call install_runtime_dependencies again with the original payload after a blocked response.
- If resolve_runtime_dependencies returns status other than "fully_installable", treat it as advisory only. Do not invent a partial install; submit a narrower explicit request only after the user has acknowledged the blocked subset. The resolver never executes anything on its own.
- In auto mode, do not ask the user to wait for authorization, manual runtime preparation, package-source approval, script binding, or other interactive choices. If a workboard item cannot pass with the current project state and safe backend tools, explicitly consume or transform it: skip the claimed todo item via skip_workboard_item, defer/block the signal with a concise reason, revise the affected card back to a planned repair state when a clear non-interactive repair exists, or create a new planned follow-up card. Do not leave the auto turn waiting for user confirmation.
- Do not create free-form todo plans in chat. If auto/background work needs sequencing, use backend workboard tools and item ids.
- Dependency ATTENTION is derived, not a persisted card status. Do not treat linked_assets as current dependency truth; use card.inputs, card.outputs, and asset.depends_on. If revise_card_plan or delete_card returns dependency_attention_check_recommended, call inspect_dependency_attention before deciding whether to continue. For input_asset_outdated issues, the old asset_id is still saved in the downstream card's inputs; if current_asset_id is clear and preserves the workflow, call revise_card_plan to replace that input asset_id first, then start_card_run in upstream-first order. Do not use rerun_card for dependency repair. If the intent is ambiguous, report the ATTENTION to the user.
- If a write tool returns ok:false, use the message/retry_hint to correct arguments and retry when the correction is clear. If it is not clear, inspect context or ask a focused question.
${webJudgmentLines.join("\n")}
- Write project memory only when the user explicitly asks you to remember a durable preference, says a behavior should be the default, or corrects something you should avoid in future. Keep memory summaries short.
- Card agents cannot ask the user interactively. If a card needs a non-default Python or R runtime, use configure_card_execution on that card before telling the user it is ready.
- Card executor agents run in a constrained runtime. They must not install missing R or Python packages on their own. If runtime packages are missing or the user explicitly wants packages added to a selected non-system runtime, you may use install_runtime_dependencies with explicit package names to start a background install job, then stop the current turn and wait for the dependency-install wake event. Include source.card_id and source.run_id when you are repairing a specific card/run. Never pass or invent a package manager; the backend resolver determines the appropriate installer based on the active policy (current default allows single-family pip / cran / bioconductor fallback, and current R dual-source fallback defaults to CRAN). If installation fails or the missing dependency is a system tool, tell the user exactly what dependency must be prepared.
- Search the skill/MCP library only when a card clearly may benefit from reusable execution abilities. The list/search tools are intentionally id/name-only; read one item detail only if the id/name is ambiguous.
- If a task looks like a stable repeated workflow, search_card_templates before creating a new analysis card from scratch.
- When a template requires script assets, ask the user which project script assets to bind before instantiate_card_template or before starting the card. Do not make card agents ask the user for bindings.
- For multi-step workflow creation, you may create multiple cards in one conversation. Re-check the timeline when useful.
- Reuse existing card ids when updating existing work. Create new ids only for genuinely new cards.
- Do not use or mention blueprint proposal, blueprint review, or approval flows. Card tools are the source of truth for blueprint edits.
- Do not restate the full DAG in chat; focus on the selected card, immediate blockers, and the next action.
- Respect selected_context.script_preference when creating analysis cards. It is a soft script-language preference, not a hard constraint.
- Respect selected_context.python_runtime and selected_context.r_runtime as preferred execution runtimes when planning or updating analysis cards.
- If script_preference is auto and a new bioinformatics card could reasonably be implemented in either Python or R, ask the user which script style they prefer when that choice materially affects the workflow.
- When a concrete script preference is known, reflect it in the card plan and chosen implementation approach, but do not send executor_context fields in normal card write payloads.
- When creating or revising an analysis card and selected_context.script_preference is prefer_python, prefer_r, or prefer_mixed, persist it into card.executor_context by calling configure_card_execution with the matching instruction_blocks entry. Do not rely on chat-side acknowledgment alone; the executor does not read chat history.
- Keep final replies concise and user-facing.

Card fields:
- create_card requires title, summary, and usually outputs.
- revise_card_plan requires exact card_id; it is a selector, not a replacement identity field.
- annotate_card requires exact card_id and is for title/summary/note changes only. Use manager_review only when replacing the displayed note is intended; use manager_review_append to add a note while preserving existing review/status text.
- step is the parallel-execution layer. Cards with no mutual dependency should share the same step. Backend validates the lower bound derived from input assets; do not pad step beyond that bound.
- Inputs are selected asset ids, shaped like { asset_id }. Use exact asset ids from find_assets or planned upstream outputs from card detail.
- Outputs are explicit semantic contracts shaped like { role, artifact_class, description? }.
- Do not send card_id on create. The backend generates it.
- Do not send card_type, why, key_findings, next_actions, linked_modules, linked_runs, linked_assets, progress_note, executor_context, accepted_formats, preferred_format, output label, output asset_id, output status, or output filenames/paths.
- Card status is not part of normal create/update payloads. New cards start as planned; later status transitions come from runs, review, delete_card, or system-derived stale state.`;
}

const TOOL_STATUS_LABELS = {
  inspect_project_summary: {
    active: "正在概览项目",
    done: "已概览项目",
  },
  get_background_workboard: {
    active: "正在读取工作板",
    done: "已读取工作板",
  },
  inspect_dependency_attention: {
    active: "正在检查依赖风险",
    done: "已检查依赖风险",
  },
  find_cards: {
    active: "正在查找卡片",
    done: "已查找卡片",
  },
  find_assets: {
    active: "正在查找资产",
    done: "已查找资产",
  },
  get_card_detail: {
    active: "正在读取卡片",
    done: "已读取卡片",
  },
  get_asset_detail: {
    active: "正在读取资产",
    done: "已读取资产",
  },
  get_project_context: {
    active: "正在查看蓝图",
    done: "已查看蓝图",
  },
  list_data_assets: {
    active: "正在查看数据资产",
    done: "已查看数据资产",
  },
  list_project_memory: {
    active: "正在读取项目记忆",
    done: "已读取项目记忆",
  },
  write_project_memory: {
    active: "正在写入项目记忆",
    done: "已写入项目记忆",
  },
  create_card: {
    active: "正在创建卡片",
    done: "已创建卡片",
  },
  revise_card_plan: {
    active: "正在更新卡片",
    done: "已更新卡片",
  },
  annotate_card: {
    active: "正在更新说明",
    done: "已更新说明",
  },
  configure_card_execution: {
    active: "正在配置卡片权限",
    done: "已配置卡片权限",
  },
  install_runtime_dependencies: {
    active: "正在提交环境依赖任务",
    done: "已提交环境依赖任务",
  },
  resolve_runtime_dependencies: {
    active: "正在解析环境依赖",
    done: "已解析环境依赖",
  },
  get_runtime_dependency_install_status: {
    active: "正在检查环境依赖任务",
    done: "已检查环境依赖任务",
  },
  promote_workboard_item_to_todo: {
    active: "正在加入待办",
    done: "已加入待办",
  },
  claim_workboard_item: {
    active: "正在领取工作项",
    done: "已领取工作项",
  },
  complete_workboard_item: {
    active: "正在完成工作项",
    done: "已完成工作项",
  },
  skip_workboard_item: {
    active: "正在跳过工作项",
    done: "已跳过工作项",
  },
  defer_workboard_item: {
    active: "正在延后工作项",
    done: "已延后工作项",
  },
  block_workboard_item_for_user: {
    active: "正在标记用户阻塞",
    done: "已标记用户阻塞",
  },
  reopen_workboard_item: {
    active: "正在重新打开工作项",
    done: "已重新打开工作项",
  },
  submit_claimed_workboard_items: {
    active: "正在提交后台批次",
    done: "已提交后台批次",
  },
  start_card_run: {
    active: "正在启动卡片",
    done: "已启动卡片",
  },
  stop_card_run: {
    active: "正在停止运行",
    done: "已停止运行",
  },
  rerun_card: {
    active: "正在重跑卡片",
    done: "已重跑卡片",
  },
  review_card_run: {
    active: "正在审核运行",
    done: "已审核运行",
  },
  cleanup_run_history: {
    active: "正在清理运行历史",
    done: "已清理运行历史",
  },
  delete_card: {
    active: "正在删除卡片",
    done: "已删除卡片",
  },
  read_result_asset: {
    active: "正在读取结果文件",
    done: "已读取结果文件",
  },
  list_skill_library: {
    active: "正在查看技能库",
    done: "已查看技能库",
  },
  search_skill_library: {
    active: "正在搜索技能库",
    done: "已搜索技能库",
  },
  get_skill_library_item: {
    active: "正在查看技能详情",
    done: "已查看技能详情",
  },
  list_mcp_library: {
    active: "正在查看 MCP 库",
    done: "已查看 MCP 库",
  },
  search_mcp_library: {
    active: "正在搜索 MCP 库",
    done: "已搜索 MCP 库",
  },
  get_mcp_library_item: {
    active: "正在查看 MCP 详情",
    done: "已查看 MCP 详情",
  },
  search_card_templates: {
    active: "正在查询模板",
    done: "已查询模板",
  },
  save_card_template: {
    active: "正在保存模板",
    done: "已保存模板",
  },
  instantiate_card_template: {
    active: "正在实例化模板",
    done: "已实例化模板",
  },
  web_search: {
    active: "正在搜索网页",
    done: "已搜索网页",
  },
  web_extract: {
    active: "正在读取网页",
    done: "已读取网页",
  },
  finish_auto_episode: {
    active: "正在完成自动轮次",
    done: "已确认完成",
  },
};

function jsonResponse(res, status, payload) {
  jsonResponseWithHeaders(res, status, payload, {});
}

function jsonResponseWithHeaders(res, status, payload, headers = {}) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    ...headers,
  });
  res.end(body);
}

const sseStreams = new WeakSet();

function openSse(res) {
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  res.flushHeaders?.();
  sseStreams.add(res);
}

function writeSseEvent(res, payload) {
  if (res.destroyed || res.writableEnded || !sseStreams.has(res)) {
    return;
  }
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(chunk);
  }
  if (!chunks.length) {
    return {};
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

async function callBackend(baseUrl, token, path, options = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: options.method || "GET",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
      ...(options.sessionId ? { "x-blueprint-session-id": options.sessionId } : {}),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  const text = await response.text();
  let payload;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { detail: text };
  }
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload);
    throw new Error(detail || `Backend tool failed with HTTP ${response.status}`);
  }
  return payload;
}

async function notifyAutoTurnSettled(baseUrl, token, projectId, sessionId, asyncBoundary, signal) {
  if (!projectId || !sessionId) {
    return null;
  }
  try {
    return await callBackend(baseUrl, token, `/projects/${projectId}/manager-auto/turn-settled`, {
      method: "POST",
      body: {
        session_id: sessionId,
        async_boundary: Boolean(asyncBoundary),
      },
      signal,
    });
  } catch (error) {
    logManagerEvent("auto_turn_settled_error", {
      project_id: projectId,
      session_id: sessionId,
      error: error instanceof Error ? error.message : String(error),
    });
    return null;
  }
}

function textResult(text, details = {}, terminate = false) {
  return {
    content: [{ type: "text", text }],
    details,
    terminate,
  };
}

function compactToolTextPayload(toolName, payload) {
  if (!payload || typeof payload !== "object") {
    return payload;
  }
  if (toolName === "get_project_context") {
    return {
      project: payload.project
        ? {
            project_id: payload.project.project_id,
            name: payload.project.name,
            current_goal: payload.project.current_goal,
          }
        : undefined,
      counts: {
        cards: Array.isArray(payload.cards) ? payload.cards.length : 0,
        modules: Array.isArray(payload.modules) ? payload.modules.length : 0,
        assets: Array.isArray(payload.assets) ? payload.assets.length : 0,
        runs: Array.isArray(payload.runs) ? payload.runs.length : 0,
        claims: Array.isArray(payload.claims) ? payload.claims.length : 0,
      },
      hint: "Full payload is stored in tool details. Prefer inspect_project_summary/find_cards/find_assets for routine planning.",
    };
  }
  if (toolName === "list_data_assets") {
    return {
      project_id: payload.project_id,
      counts: {
        assets: Array.isArray(payload.assets) ? payload.assets.length : 0,
        cards: Array.isArray(payload.cards) ? payload.cards.length : 0,
        materialized_assets: Array.isArray(payload.materialized_assets) ? payload.materialized_assets.length : 0,
        planned_assets: Array.isArray(payload.planned_assets) ? payload.planned_assets.length : 0,
      },
      duplicate_output_assets: payload.timeline?.duplicate_output_assets ?? [],
      cycle_card_ids: payload.timeline?.cycle_card_ids ?? [],
      hint: "Full payload is stored in tool details. Prefer find_assets for choosing specific asset ids.",
    };
  }
  if (toolName === "inspect_dependency_attention") {
    const hasOutdatedInput = Array.isArray(payload.dependency_attention)
      && payload.dependency_attention.some((issue) => issue && issue.kind === "input_asset_outdated" && issue.current_asset_id);
    return {
      project_id: payload.project_id,
      issue_count: payload.issue_count,
      returned_issue_count: payload.returned_issue_count,
      severity_counts: payload.severity_counts,
      dependency_attention: Array.isArray(payload.dependency_attention)
        ? payload.dependency_attention.slice(0, 12).map((issue) => ({
            issue_id: issue.issue_id,
            kind: issue.kind,
            severity: issue.severity,
            card_id: issue.card_id,
            asset_id: issue.asset_id,
            current_asset_id: issue.current_asset_id,
            producer_card_id: issue.producer_card_id,
            message: issue.message,
          }))
        : [],
      affected_downstream: Array.isArray(payload.affected_downstream) ? payload.affected_downstream.slice(0, 12) : [],
      repair_execution_order: payload.repair_execution_order,
      manager_repair_guidance: hasOutdatedInput
        ? "For input_asset_outdated, use revise_card_plan to replace the affected downstream input asset_id from asset_id to current_asset_id before start_card_run. Do not use rerun_card for dependency repair."
        : undefined,
      truncated: payload.truncated,
    };
  }
  if (toolName === "get_background_workboard") {
    return {
      project_id: payload.project_id,
      revision: payload.revision,
      counts: payload.counts,
      running: compactItems(payload.running, (item) => ({
        item_id: item.item_id,
        task_id: item.task_id,
        kind: item.kind,
        card_id: item.card_id,
        run_id: item.run_id,
        job_id: item.job_id,
        status: item.status,
      }), 8),
      todo: compactItems(payload.todo, (item) => ({
        item_id: item.item_id,
        kind: item.kind,
        card_id: item.card_id,
        source_item_id: item.source_item_id,
        status: item.status,
      }), 8),
      needs_manager: compactItems(payload.needs_manager, (item) => ({
        item_id: item.item_id,
        kind: item.kind,
        card_id: item.card_id,
        run_id: item.run_id,
        job_id: item.job_id,
        recommended_action: item.recommended_action,
        message: truncateText(item.message, 140),
      }), 8),
      completed: compactItems(payload.completed, (item) => ({
        item_id: item.item_id,
        kind: item.kind,
        card_id: item.card_id,
        run_id: item.run_id,
        summary: truncateText(item.summary, 140),
      }), 8),
      ready_to_start: compactItems(payload.ready_to_start, (item) => ({
        item_id: item.item_id,
        card_id: item.card_id,
        title: item.title,
        parallel_group: item.payload?.parallel_group,
      }), 8),
      blocked_for_user: compactItems(payload.blocked_for_user, (item) => ({
        item_id: item.item_id,
        kind: item.kind,
        message: truncateText(item.message, 140),
      }), 8),
      deferred: compactItems(payload.deferred, (item) => ({
        item_id: item.item_id,
        kind: item.kind,
        message: truncateText(item.message, 140),
      }), 8),
    };
  }
  if (
    toolName === "promote_workboard_item_to_todo" ||
    toolName === "claim_workboard_item" ||
    toolName === "complete_workboard_item" ||
    toolName === "defer_workboard_item" ||
    toolName === "block_workboard_item_for_user" ||
    toolName === "reopen_workboard_item"
  ) {
    return {
      ok: payload.ok,
      item: payload.item
        ? {
            item_id: payload.item.item_id,
            lane: payload.item.lane,
            kind: payload.item.kind,
            status: payload.item.status,
            card_id: payload.item.card_id,
            source_item_id: payload.item.source_item_id,
          }
        : undefined,
    };
  }
  if (toolName === "submit_claimed_workboard_items") {
    return {
      ok: payload.ok,
      task_id: payload.task_id,
      run_id: payload.run_id,
      started_count: payload.started_count,
      batch: payload.batch,
      started: compactItems(payload.started, (item) => ({
        item_id: item.item_id,
        card_id: item.card_id,
        run_id: item.run_id,
        task_id: item.task_id,
      }), 8),
      blocked: compactItems(payload.blocked, (item) => item, 8),
      async_boundary: payload.async_boundary,
      wait_for_wake: payload.wait_for_wake,
      message: payload.message,
    };
  }
  if (["create_card", "revise_card_plan", "annotate_card", "delete_card", "configure_card_execution"].includes(toolName)) {
    return {
      ok: payload.ok ?? true,
      card_id: payload.card_id ?? payload.card?.card_id,
      card: payload.card ? compactCardForText(payload.card) : undefined,
      updated_card_ids: payload.updated_card_ids,
      message: payload.message,
      dependency_attention_check_recommended: payload.dependency_attention_check_recommended,
      affected_downstream: Array.isArray(payload.affected_downstream) ? payload.affected_downstream.slice(0, 8) : undefined,
      recommended_next_tool: payload.recommended_next_tool,
      repair_execution_order_hint: payload.repair_execution_order_hint,
    };
  }
  if (payload.asset || payload.preview) {
    return {
      asset: payload.asset
        ? {
            asset_id: payload.asset.asset_id,
            title: payload.asset.title,
            status: payload.asset.status,
            asset_type: payload.asset.asset_type,
            path: payload.asset.path,
          }
        : undefined,
      preview: payload.preview
        ? {
            kind: payload.preview.kind,
            size_bytes: payload.preview.size_bytes,
            truncated: payload.preview.truncated,
          }
        : undefined,
    };
  }
  return payload;
}

function compactCardForText(card) {
  if (!card || typeof card !== "object") {
    return card;
  }
  return {
    card_id: card.card_id,
    title: card.title,
    status: card.status,
    card_type: card.card_type,
    step: card.step,
    summary: card.summary,
    progress_note: card.progress_note,
    inputs: Array.isArray(card.inputs)
      ? card.inputs.map((item) => ({ label: item.label, asset_id: item.asset_id, status: item.status }))
      : undefined,
    outputs: Array.isArray(card.outputs)
      ? card.outputs.map((item) => ({
          role: item.role,
          label: item.label,
          artifact_class: item.artifact_class,
          asset_id: item.asset_id,
          status: item.status,
        }))
      : undefined,
  };
}

function truncateText(value, maxLength = 180) {
  if (typeof value !== "string") {
    return value;
  }
  const compact = value.replace(/\s+/g, " ").trim();
  if (compact.length <= maxLength) {
    return compact;
  }
  return `${compact.slice(0, Math.max(0, maxLength - 3)).trimEnd()}...`;
}

function compactItems(items, mapItem, limit = 5) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items.slice(0, Math.max(1, limit)).map((item) => mapItem(item));
}

function toolTextResult(toolName, payload, terminate = false) {
  return textResult(JSON.stringify(compactToolTextPayload(toolName, payload), null, 2), payload, terminate);
}

function toolErrorResult(error, context = {}) {
  const message = error instanceof Error ? error.message : String(error);
  const payload = {
    ok: false,
    error_type: context.error_type || "tool_error",
    message,
    retry_hint: context.retry_hint || retryHintForToolError(message),
    should_retry: context.should_retry ?? isRetryableToolError(message),
    ...context,
  };
  return textResult(JSON.stringify(payload, null, 2), payload, false);
}

function isRetryableToolError(message) {
  return /step too early|input asset .*missing|duplicate card_id|duplicate planned output|card not found|asset_id|required|title is required|summary is required/i.test(message);
}

function retryHintForToolError(message) {
  if (/step too early/i.test(message)) {
    return "Increase the card step to the minimum required by the error, then retry the same write tool.";
  }
  if (/input asset .*missing|asset_id/i.test(message)) {
    return "Call find_assets to find the correct asset_id, or create an upstream card output first.";
  }
  if (/duplicate card_id/i.test(message)) {
    return "Use revise_card_plan or annotate_card for the existing card, or choose a new card_id for genuinely new work.";
  }
  if (/duplicate planned output/i.test(message)) {
    return "Reuse the existing planned asset_id as an input, or choose a distinct output asset_id.";
  }
  if (/card not found/i.test(message)) {
    return "Call find_cards to locate the card_id, then get_card_detail before retrying.";
  }
  if (/title is required|summary is required|required/i.test(message)) {
    return "Fill the required card fields and retry.";
  }
  if (/Source-install dependencies are not supported|github_source_install_not_supported|external_source_install_not_supported/i.test(message)) {
    return "Do not retry the normal dependency installer. Use a supported registry-backed package name or tell the user this requires a separate source-install environment workflow.";
  }
  return "Inspect the error and retry with corrected arguments if the correction is clear.";
}

function dependencyInstallRetryHint(details = {}) {
  if (!details || typeof details !== "object") {
    return null;
  }
  const errorCode = details.error_code || "";
  const status = details.status || "";
  const fallback = Array.isArray(details.fallback_available) ? details.fallback_available.filter(Boolean) : [];
  const attempted = Array.isArray(details.attempted_candidates) ? details.attempted_candidates.filter(Boolean) : [];
  const requestedPackage = details.requested_package || "the requested package";
  if (status === "fallback_available_but_ambiguous" || errorCode === "fallback_available_but_ambiguous") {
    const fallbackText = fallback.length ? ` Possible fallback families: ${fallback.join(", ")}.` : "";
    return `Fallback families exist, but the resolver could not reduce the request to one safe registry family even after applying its built-in preferences. Do not guess pip/cran/bioconductor or retry the same payload; ask for manual runtime preparation or a narrower explicit request after review.${fallbackText}`;
  }
  if (errorCode === "package_not_found_in_conda_channels") {
    const fallbackText = fallback.length ? ` Possible manual fallback families: ${fallback.join(", ")}.` : "";
    return `Conda channels do not contain ${requestedPackage}. Do not retry install_runtime_dependencies with a manager argument; verify the real distribution name or tell the user manual environment preparation is needed. Attempted candidates: ${attempted.join(", ") || "none"}.${fallbackText}`;
  }
  if (errorCode === "github_source_install_not_supported" || errorCode === "external_source_install_not_supported") {
    return "Do not retry install_runtime_dependencies with source URLs, GitHub repos, or tarballs. Use a registry-backed package name or ask for a separate manual environment-preparation workflow.";
  }
  if (errorCode === "dependency_install_compilation_failed") {
    if (fallback.length) {
      return `The source-style install path failed during compilation. Do not retry with a manager argument; use backend default conda-family install or tell the user the runtime needs manual preparation.`;
    }
    return "The install failed during compilation. Prefer a binary-backed package source when possible, or tell the user the runtime needs manual preparation.";
  }
  if (errorCode === "duplicate_dependency_resolution_in_progress") {
    return `The same dependency installation is already running for this runtime (prior job: ${details.prior_job_id || "unknown"}). Do not start another install_runtime_dependencies for the same package/runtime combination. Wait for the existing job to finish or for a wake event.`;
  }
  if (errorCode === "duplicate_dependency_resolution_failure") {
    const priorError = details.prior_error_code || "unknown";
    const fallbackText = fallback.length ? ` Possible manual fallback families: ${fallback.join(", ")}.` : "";
    return `The same dependency request already failed for this runtime (prior job: ${details.prior_job_id || "unknown"}, prior error: ${priorError}). Do not retry install_runtime_dependencies with the same package/runtime. Modify the package list, change the runtime, or ask the user to prepare the environment manually.${fallbackText}`;
  }
  if (errorCode === "partial_resolution_requires_manual_preparation") {
    return `The resolver blocked execution: some packages are not installable through the configured conda channels. Do not retry install_runtime_dependencies with the original payload. Ask the user to manually prepare the blocked packages, or submit a narrower explicit request for only the installable subset.`;
  }
  if (errorCode === "fallback_available_but_policy_disallows") {
    const fallbackText = fallback.length ? ` Possible manual fallback families: ${fallback.join(", ")}.` : "";
    return `Fallback families exist for the unresolved packages, but the active policy is report_only and will not auto-execute them.${fallbackText} Ask the user to manually prepare the runtime, or wait for an explicit allow_safe_registry_install policy.`;
  }
  if (errorCode === "manual_preparation_required") {
    return "The dependency request cannot proceed automatically; ask the user to prepare the runtime manually before retrying.";
  }
  if (errorCode === "unsupported_source_spec" || errorCode === "github_source_install_not_supported" || errorCode === "external_source_install_not_supported") {
    return "Do not retry install_runtime_dependencies with source URLs, GitHub repos, or tarballs. Use a registry-backed package name or ask for a separate manual environment-preparation workflow.";
  }
  if (errorCode === "dependency_install_start_failed" || errorCode === "runtime_missing") {
    return "The selected runtime path could not be resolved or started. Verify the runtime exists, then retry; otherwise ask the user to prepare the runtime manually.";
  }
  if (errorCode === "dependency_probe_failed" || status === "solver_error") {
    return "The conda solver probe failed (e.g. proxy error, no-writable-pkgs-dir, or bad conda config). This is an environment problem, not a missing package. Do not retry install_runtime_dependencies or fall back to CRAN/pip automatically. Inspect stderr for the exact solver error and ask the user to fix the conda environment before retrying.";
  }
  if (errorCode === "dependency_resolution_unknown") {
    return "The resolver could not classify the request (the underlying probe was inconclusive). Inspect the resolver plan and ask the user to confirm the package list before retrying.";
  }
  if (errorCode === "mixed_installer_rejected") {
    return "Mixed-installer plans (e.g. conda + pip, or cran + bioconductor) are rejected. Narrow the request to a single installer family, or ask the user to split it into separate single-family calls.";
  }
  if (errorCode === "unsupported_installer") {
    return "The installer type requested is not supported. Ask the user to select a supported package manager or prepare the runtime manually.";
  }
  return null;
}

function scriptPreferenceGuidance(scriptPreference) {
  const value = ["prefer_python", "prefer_r", "prefer_mixed", "auto"].includes(scriptPreference) ? scriptPreference : "auto";
  const instructions = {
    auto:
      "No script language preference is set. If creating new bioinformatics analysis cards and Python vs R materially changes implementation quality or runtime dependency choices, ask the user which script style they prefer.",
    prefer_python:
      "Soft script preference: prefer Python scripts when practical. This is not a hard constraint; use R when it is more reliable or better supported for this task.",
    prefer_r:
      "Soft script preference: prefer R scripts when practical. This is not a hard constraint; use Python when it is more reliable or better supported for this task.",
    prefer_mixed:
      "Soft script preference: choose Python or R per task based on reliability, available runtime dependencies, and clearer reproducible code.",
  };
  return {
    value,
    card_instruction_block: instructions[value],
    hard_constraint: false,
  };
}

function runtimePreferenceGuidance(context = {}) {
  const pythonRuntime = context?.python_runtime || null;
  const rRuntime = context?.r_runtime || null;
  const instructions = [];
  if (pythonRuntime) {
    instructions.push(`Preferred Python runtime for future card execution: ${pythonRuntime}.`);
  }
  if (rRuntime) {
    instructions.push(`Preferred R runtime for future card execution: ${rRuntime}.`);
  }
  if (!instructions.length) {
    return { python_runtime: null, r_runtime: null, card_instruction_block: null };
  }
  return {
    python_runtime: pythonRuntime,
    r_runtime: rRuntime,
    card_instruction_block: `Runtime preference: ${instructions.join(" ")} Use it when planning or choosing runtimes for new or updated analysis cards.`,
  };
}

function logManagerEvent(event, fields = {}) {
  console.log(
    JSON.stringify({
      ts: new Date().toISOString(),
      scope: "manager-agent",
      event,
      ...fields,
    }),
  );
}

function payloadSize(payload) {
  try {
    return Buffer.byteLength(JSON.stringify(payload ?? {}));
  } catch {
    return null;
  }
}

function summarizeToolPayload(toolName, payload) {
  if (!payload || typeof payload !== "object") {
    return {};
  }
  if (toolName === "inspect_project_summary") {
    return {
      cards: payload.counts?.cards,
      materialized_assets: payload.counts?.materialized_assets,
      planned_assets: payload.counts?.planned_assets,
      blockers: payload.counts?.blockers,
      dependency_attention: payload.counts?.dependency_attention,
    };
  }
  if (toolName === "get_background_workboard") {
    return {
      revision: payload.revision,
      counts: payload.counts,
      ready_to_start: Array.isArray(payload.ready_to_start) ? payload.ready_to_start.length : undefined,
      todo: Array.isArray(payload.todo) ? payload.todo.length : undefined,
      needs_manager: Array.isArray(payload.needs_manager) ? payload.needs_manager.length : undefined,
      completed: Array.isArray(payload.completed) ? payload.completed.length : undefined,
      running: Array.isArray(payload.running) ? payload.running.length : undefined,
    };
  }
  if (toolName === "inspect_dependency_attention") {
    return {
      issue_count: payload.issue_count,
      returned_issue_count: payload.returned_issue_count,
      affected_downstream: Array.isArray(payload.affected_downstream) ? payload.affected_downstream.length : undefined,
      repair_execution_order: Array.isArray(payload.repair_execution_order) ? payload.repair_execution_order.length : undefined,
    };
  }
  if (toolName === "find_cards" || toolName === "find_assets") {
    return {
      items: Array.isArray(payload.items) ? payload.items.length : undefined,
      total: payload.total,
    };
  }
  if (toolName === "get_card_detail") {
    return {
      card_id: payload.card?.card_id,
      status: payload.card?.status,
      runs: Array.isArray(payload.runs) ? payload.runs.length : undefined,
      dependency_attention: Array.isArray(payload.dependency_attention) ? payload.dependency_attention.length : undefined,
    };
  }
  if (toolName === "get_asset_detail") {
    return {
      asset_id: payload.asset?.asset_id,
      preview_kind: payload.preview?.kind,
    };
  }
  if (toolName === "get_project_context") {
    return {
      cards: Array.isArray(payload.cards) ? payload.cards.length : undefined,
      modules: Array.isArray(payload.modules) ? payload.modules.length : undefined,
      assets: Array.isArray(payload.assets) ? payload.assets.length : undefined,
    };
  }
  if (toolName === "list_data_assets") {
    return {
      assets: Array.isArray(payload.assets) ? payload.assets.length : undefined,
      cards: Array.isArray(payload.cards) ? payload.cards.length : undefined,
      planned_assets: Array.isArray(payload.planned_assets) ? payload.planned_assets.length : undefined,
    };
  }
  if (toolName === "list_project_memory") {
    return {
      memory_items: Array.isArray(payload.items) ? payload.items.length : undefined,
      items: compactItems(payload.items, (item) => ({
        memory_id: item.memory_id,
        kind: item.kind,
        summary: truncateText(item.summary, 160),
      })),
    };
  }
  if (toolName === "write_project_memory") {
    return {
      ok: true,
      memory: payload.memory
        ? {
            memory_id: payload.memory.memory_id,
            kind: payload.memory.kind,
            summary: truncateText(payload.memory.summary, 160),
          }
        : undefined,
      items_count: payload.items_count,
    };
  }
  if (toolName === "install_runtime_dependencies" || toolName === "get_runtime_dependency_install_status") {
    return {
      task_id: payload.task_id,
      job_id: payload.job_id,
      status: payload.status,
      runtime: payload.runtime,
      package_count: Array.isArray(payload.packages) ? payload.packages.length : undefined,
      packages: compactItems(payload.packages, (item) => item, 5),
      background: payload.background,
      ok: payload.ok,
      requested_package: payload.requested_package,
      error_code: payload.error_code,
      attempted_candidates: compactItems(payload.attempted_candidates, (item) => item, 5),
      fallback_available: compactItems(payload.fallback_available, (item) => item, 5),
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "resolve_runtime_dependencies") {
    return {
      ok: payload.ok,
      status: payload.status,
      runtime: payload.runtime,
      ecosystem: payload.ecosystem,
      installable_count: Array.isArray(payload.installable) ? payload.installable.length : undefined,
      blocked_count: Array.isArray(payload.blocked) ? payload.blocked.length : undefined,
      installable: compactItems(payload.installable, (item) => item, 5),
      blocked: compactItems(payload.blocked, (item) => ({
        name: item?.name,
        reason: item?.reason,
        fallback_available: item?.fallback_available,
      }), 5),
      fallback_policy: payload.fallback_policy,
      in_flight_duplicate: payload.in_flight_duplicate,
      terminal_duplicate: payload.terminal_duplicate,
      retry_hint: payload.retry_hint,
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "start_card_run" || toolName === "rerun_card") {
    return {
      ok: payload.ok,
      task_id: payload.task_id,
      run_id: payload.run_id,
      card_id: payload.card_id,
      status: payload.status,
      background: payload.background,
      async_boundary: payload.async_boundary,
      do_not_poll: payload.do_not_poll,
      wait_for_wake: payload.wait_for_wake,
      can_start: payload.can_start,
      pending_approvals_count: Array.isArray(payload.pending_approvals) ? payload.pending_approvals.length : undefined,
      rejected_approvals_count: Array.isArray(payload.rejected_approvals) ? payload.rejected_approvals.length : undefined,
      block_reasons: compactItems(payload.block_reasons, (item) => item, 4),
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "stop_card_run") {
    return {
      ok: payload.ok,
      card_id: payload.card_id,
      stopped: payload.stopped,
      stopped_run_ids: compactItems(payload.stopped_run_ids, (item) => item, 6),
      failed_results: Array.isArray(payload.failed_results) ? payload.failed_results.length : undefined,
      message: truncateText(payload.message, 180),
    };
  }
  if (
    toolName === "promote_workboard_item_to_todo" ||
    toolName === "claim_workboard_item" ||
    toolName === "complete_workboard_item" ||
    toolName === "defer_workboard_item" ||
    toolName === "block_workboard_item_for_user" ||
    toolName === "reopen_workboard_item"
  ) {
    return {
      ok: payload.ok,
      item: payload.item
        ? {
            item_id: payload.item.item_id,
            lane: payload.item.lane,
            kind: payload.item.kind,
            status: payload.item.status,
            card_id: payload.item.card_id,
            source_item_id: payload.item.source_item_id,
          }
        : undefined,
    };
  }
  if (toolName === "submit_claimed_workboard_items") {
    return {
      ok: payload.ok,
      task_id: payload.task_id,
      run_id: payload.run_id,
      started_count: payload.started_count,
      approval_required_count: payload.approval_required_count,
      rejected_count: payload.rejected_count,
      background_started_count: payload.background_started_count,
      batch: payload.batch,
      started: Array.isArray(payload.started) ? payload.started.length : undefined,
      blocked: Array.isArray(payload.blocked) ? payload.blocked.length : undefined,
      async_boundary: payload.async_boundary,
      wait_for_wake: payload.wait_for_wake,
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "review_card_run") {
    return {
      ok: payload.ok,
      run_id: payload.run_id,
      card_id: payload.card_id,
      review_completed: payload.review_completed,
      accepted: payload.accepted,
      failure_reason: payload.failure_reason,
      failure_details: Array.isArray(payload.failure_details) ? payload.failure_details.length : undefined,
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "cleanup_run_history") {
    return {
      cleaned_count: payload.cleaned_count,
      skipped_count: payload.skipped_count,
      dry_run: payload.dry_run,
      ok: payload.ok,
      cleaned: compactItems(payload.cleaned, (item) => ({
        run_id: item.run_id,
        card_id: item.card_id,
        status: item.status,
      })),
      skipped: compactItems(payload.skipped, (item) => ({
        run_id: item.run_id,
        card_id: item.card_id,
        status: item.status,
        reason: truncateText(item.reason, 100),
      })),
      message: truncateText(payload.message, 180),
    };
  }
  if (toolName === "search_card_templates") {
    return {
      templates: Array.isArray(payload.items) ? payload.items.length : undefined,
      total: payload.total,
      items: compactItems(payload.items, (item) => ({
        template_id: item.template_id,
        title: item.title,
        card_type: item.card_type,
        score: item.score,
        tags: Array.isArray(item.tags) ? item.tags.slice(0, 4) : [],
      })),
    };
  }
  if (
    toolName === "list_skill_library" ||
    toolName === "list_mcp_library" ||
    toolName === "search_skill_library" ||
    toolName === "search_mcp_library"
  ) {
    return {
      items: Array.isArray(payload.items) ? payload.items.length : undefined,
      entries: compactItems(payload.items, (item) => ({
        id: item.id,
        name: item.name,
        enabled: item.enabled,
        score: item.score,
      })),
    };
  }
  if (toolName === "get_skill_library_item" || toolName === "get_mcp_library_item") {
    return {
      kind: payload.kind,
      item: payload.item
        ? {
            id: payload.item.id,
            name: payload.item.name,
            enabled: payload.item.enabled,
            summary_short: truncateText(payload.item.summary_short, 180),
            supported_runtimes: Array.isArray(payload.item.supported_runtimes) ? payload.item.supported_runtimes.slice(0, 5) : [],
          }
        : undefined,
    };
  }
  if (toolName === "save_card_template") {
    return {
      ok: payload.ok,
      template: payload.template
        ? {
            template_id: payload.template.template_id,
            title: payload.template.title,
            card_type: payload.template.card_type,
            source_card_id: payload.template.source_card_id,
          }
        : undefined,
    };
  }
  if (toolName === "instantiate_card_template") {
    return {
      ok: payload.ok ?? true,
      template_id: payload.template_id,
      card: payload.card ? compactCardForText(payload.card) : undefined,
    };
  }
  if (toolName === "web_search") {
    return {
      answer: truncateText(payload.answer || payload.summary, 280),
      results: compactItems(
        payload.results,
        (item) => ({
          title: truncateText(item.title, 120),
          url: item.url,
          score: item.score,
          snippet: truncateText(item.content || item.snippet, 180),
        }),
        4,
      ),
    };
  }
  if (toolName === "web_extract") {
    return {
      results: compactItems(
        payload.results,
        (item) => ({
          url: item.url,
          title: truncateText(item.title, 120),
          excerpt: truncateText(item.raw_content || item.content, 220),
        }),
        3,
      ),
      failed_results: Array.isArray(payload.failed_results) ? payload.failed_results.length : undefined,
    };
  }
  if (toolName === "create_card" || toolName === "revise_card_plan" || toolName === "annotate_card" || toolName === "delete_card") {
    return {
      card_id: payload.card?.card_id,
      card_status: payload.card?.status,
      card_step: payload.card?.step,
      timeline_cards: Array.isArray(payload.timeline?.cards) ? payload.timeline.cards.length : undefined,
      timeline_assets: Array.isArray(payload.timeline?.assets) ? payload.timeline.assets.length : undefined,
      dependency_attention_check_recommended: payload.dependency_attention_check_recommended,
      affected_downstream: Array.isArray(payload.affected_downstream) ? payload.affected_downstream.length : undefined,
    };
  }
  if (payload.asset) {
    return {
      asset_id: payload.asset.asset_id,
      preview_kind: payload.preview?.kind,
      size_bytes: payload.preview?.size_bytes,
    };
  }
  return {};
}

function buildToolReport(toolName, details) {
  if (!details || typeof details !== "object") {
    return null;
  }
  if (toolName === "get_background_workboard") {
    return {
      summary: "已读取后台工作板。",
      details,
    };
  }
  if (toolName === "install_runtime_dependencies" && details.background && details.job_id) {
    const runtime = details.runtime || "selected runtime";
    const packageCount = Array.isArray(details.packages) ? details.packages.length : 0;
    return {
      summary:
        details.message ||
        `已启动后台依赖安装任务：${runtime}${packageCount ? `，共 ${packageCount} 个包` : ""}。`,
      details,
    };
  }
  if (toolName === "install_runtime_dependencies" && details.ok === false) {
    const requestedPackage = details.requested_package ? ` 目标包：${details.requested_package}。` : "";
    const attempted = Array.isArray(details.attempted_candidates) && details.attempted_candidates.length
      ? ` 候选尝试：${details.attempted_candidates.join(", ")}。`
      : "";
    const fallback = Array.isArray(details.fallback_available) && details.fallback_available.length
      ? ` 可尝试：${details.fallback_available.join(", ")}。`
      : "";
    const retryHint = dependencyInstallRetryHint(details);
    return {
      summary:
        details.message ||
        `依赖安装未启动：${details.error_code || "unknown_error"}。${requestedPackage}${attempted}${fallback}`.trim(),
      details: retryHint ? { ...details, retry_hint: retryHint } : details,
    };
  }
  if (toolName === "resolve_runtime_dependencies" && details.status) {
    const blocked = Array.isArray(details.blocked) ? details.blocked : [];
    const installable = Array.isArray(details.installable) ? details.installable : [];
    const blockedNames = blocked
      .map((entry) => entry && (entry.name || entry["name"]))
      .filter(Boolean);
    const blockedText = blockedNames.length ? ` 阻塞：${blockedNames.join(", ")}。` : "";
    const installableText = installable.length ? ` 可装：${installable.length} 个。` : "";
    const installers = [...new Set(installable.map((entry) => entry && entry.installer).filter(Boolean))];
    const installerText = installers.length ? ` installer：${installers.join(", ")}。` : "";
    const summary =
      details.status === "fully_installable"
        ? `依赖解析通过，全部 ${installable.length || 0} 个包可由 resolver 批准的安装器处理。${installerText}`.trim()
        : `依赖解析被阻止（${details.status}）。${installableText}${blockedText}`.trim();
    return { summary, details };
  }
  if ((toolName === "start_card_run" || toolName === "rerun_card") && details.background && details.run_id) {
    return {
      summary:
        details.message ||
        `已启动后台运行 ${details.run_id}。本轮不要轮询卡片状态，等待运行事件或 wake 事件继续。`,
      details,
    };
  }
  if ((toolName === "start_card_run" || toolName === "rerun_card") && details.run_id) {
    return {
      summary:
        details.message ||
        `运行 ${details.run_id} 需要先处理审批或其他前置条件，本轮继续处理，不进入后台等待。`,
      details,
    };
  }
  if (toolName === "finish_auto_episode" && details.ok) {
    return {
      summary: `自动轮次已确认完成。${details.complete_message || ""}`,
    };
  }
  if (toolName === "submit_claimed_workboard_items" && details.background && details.run_id) {
    const startedCount = Number(details.started_count || 1);
    const batchLabel = startedCount > 1
      ? `已从工作板提交后台批次，共 ${startedCount} 个运行，首个运行 ${details.run_id}。`
      : `已从工作板提交后台运行 ${details.run_id}。`;
    return {
      summary:
        details.message ||
        `${batchLabel}本轮不要轮询状态，等待事件或 wake 继续。`,
      details,
    };
  }
  if (toolName === "submit_claimed_workboard_items" && details.started_count) {
    return {
      summary:
        details.message ||
        `工作板批次已提交 ${details.started_count} 个运行，但仍有审批或前置条件需要当前回合处理。`,
      details,
    };
  }
  if (toolName === "get_runtime_dependency_install_status" && details.job_id) {
    const status = details.status || "unknown";
    const requestedPackage = details.requested_package ? ` 目标包：${details.requested_package}。` : "";
    const attempted = Array.isArray(details.attempted_candidates) && details.attempted_candidates.length
      ? ` 候选尝试：${details.attempted_candidates.join(", ")}。`
      : "";
    const fallback = Array.isArray(details.fallback_available) && details.fallback_available.length
      ? ` 可尝试：${details.fallback_available.join(", ")}。`
      : "";
    const retryHint = status === "failed" ? dependencyInstallRetryHint(details) : null;
    return {
      summary:
        details.message ||
        (status === "succeeded"
          ? "后台依赖安装已完成。"
          : status === "failed"
            ? `后台依赖安装失败。${requestedPackage}${attempted}${fallback}`.trim()
            : "后台依赖安装仍在进行。"),
      details: retryHint ? { ...details, retry_hint: retryHint } : details,
    };
  }
  return null;
}

function buildAsyncBoundaryTurnMessage(turnControl) {
  const boundary = turnControl?.async_boundary;
  if (!boundary?.active) {
    return "";
  }
  if (boundary.toolName === "install_runtime_dependencies") {
    return boundary.jobId
      ? `已提交环境依赖任务（${boundary.jobId}），等待后台完成后继续。`
      : "已提交环境依赖任务，等待后台完成后继续。";
  }
  if (boundary.batch) {
    return boundary.runId
      ? `已提交后台运行批次（首个 run_id: ${boundary.runId}），等待事件后继续。`
      : "已提交后台运行批次，等待事件后继续。";
  }
  if (boundary.runId) {
    return `已启动后台运行（${boundary.runId}），等待事件后继续。`;
  }
  if (boundary.jobId) {
    return `已启动后台任务（${boundary.jobId}），等待事件后继续。`;
  }
  return "已启动后台任务，等待事件后继续。";
}

function isRunAsyncBoundaryPayload(toolName, payload) {
  if (toolName !== "start_card_run" && toolName !== "rerun_card" && toolName !== "submit_claimed_workboard_items") {
    return false;
  }
  if (!payload || typeof payload !== "object") {
    return false;
  }
  if (payload.status === "needs_approval") {
    return false;
  }
  if ((Array.isArray(payload.pending_approvals) && payload.pending_approvals.length) || (Array.isArray(payload.rejected_approvals) && payload.rejected_approvals.length)) {
    return false;
  }
  if (Number(payload.approval_required_count || 0) > 0 || Number(payload.rejected_count || 0) > 0) {
    return false;
  }
  if (!payload.ok || !payload.run_id || !payload.async_boundary || !payload.wait_for_wake) {
    return false;
  }
  return true;
}

function isDependencyJobAsyncBoundaryPayload(toolName, payload) {
  if (toolName !== "install_runtime_dependencies") {
    return false;
  }
  if (!payload || typeof payload !== "object") {
    return false;
  }
  return Boolean(payload.ok && payload.job_id && payload.background && payload.async_boundary && payload.wait_for_wake);
}

async function callBackendLoggedTool(toolName, toolCallId, projectId, baseUrl, token, path, options = {}, signal, sessionId = null) {
  const startedAt = Date.now();
  logManagerEvent("tool_backend_start", {
    project_id: projectId,
    tool_name: toolName,
    tool_call_id: toolCallId,
    method: options.method || "GET",
    path,
    request_bytes: payloadSize(options.body),
  });
  try {
    const payload = await callBackend(baseUrl, token, path, { ...options, signal, sessionId });
    logManagerEvent("tool_backend_done", {
      project_id: projectId,
      tool_name: toolName,
      tool_call_id: toolCallId,
      duration_ms: Date.now() - startedAt,
      response_bytes: payloadSize(payload),
      ...summarizeToolPayload(toolName, payload),
    });
    return payload;
  } catch (error) {
    logManagerEvent("tool_backend_error", {
      project_id: projectId,
      tool_name: toolName,
      tool_call_id: toolCallId,
      duration_ms: Date.now() - startedAt,
      error: error instanceof Error ? error.message : String(error),
    });
    throw error;
  }
}

function createTools(request, runtimeConfig = resolveManagerConfig(request)) {
  const { project_id: projectId, backend_api_base_url: baseUrl, internal_tool_token: token, session_id: sessionId } = request;
  const autoMode = request.auto_mode && typeof request.auto_mode === "object" ? request.auto_mode : {};
  const btwMode = Boolean(autoMode.btw_mode);
  const userRequestedInterrupt = /stop|cancel|interrupt|abort|停止|取消|中断|打断/.test(String(request.message || "").toLowerCase());
  // Per-turn boundary only. A later wake turn or user-initiated turn gets a
  // fresh tool set and may inspect state for its new reason.
  const asyncBoundary = {
    active: false,
    toolName: null,
    runId: null,
    jobId: null,
    startedCount: 0,
    batch: false,
    installOnly: false,
  };
  const NON_YIELDING_TOOLS = new Set([
    "inspect_project_summary", "inspect_dependency_attention",
    "find_cards", "find_assets",
    "get_card_detail", "get_asset_detail", "get_project_context",
    "get_background_workboard", "get_runtime_dependency_install_status",
    "get_skill_library_item", "get_mcp_library_item",
    "list_data_assets", "list_project_memory", "list_skill_library", "list_mcp_library",
    "search_card_templates", "search_skill_library", "search_mcp_library",
    "read_result_asset", "read_project_memory", "search_project_memory",
    "configure_card_execution", "annotate_card", "write_project_memory",
  ]);
  const callLoggedTool = async (toolName, toolCallId, projectId, baseUrl, token, path, options = {}, signal, sessionId = null) => {
    if (asyncBoundary.active && !(toolName === "stop_card_run" && userRequestedInterrupt)) {
      // Non-yielding tools are always allowed after async boundary
      if (NON_YIELDING_TOOLS.has(toolName)) {
        return await callBackendLoggedTool(toolName, toolCallId, projectId, baseUrl, token, path, options, signal, sessionId);
      }
      // Allow one run-yielding action after an install-only boundary
      const isRunYielding = ["start_card_run", "rerun_card", "submit_claimed_workboard_items"].includes(toolName);
      if (asyncBoundary.installOnly && isRunYielding) {
        asyncBoundary.installOnly = false;
        const payload = await callBackendLoggedTool(toolName, toolCallId, projectId, baseUrl, token, path, options, signal, sessionId);
        if (isRunAsyncBoundaryPayload(toolName, payload)) {
          asyncBoundary.toolName = toolName;
          asyncBoundary.runId = payload.run_id;
          asyncBoundary.startedCount = Number(payload.started_count || 1);
          asyncBoundary.batch = Boolean(payload.batch || (toolName === "submit_claimed_workboard_items" && asyncBoundary.startedCount > 1));
        }
        return payload;
      }
      const backgroundLabel = asyncBoundary.batch
        ? `Background run batch (${asyncBoundary.startedCount || 0} runs${asyncBoundary.runId ? `, first run ${asyncBoundary.runId}` : ""})`
        : asyncBoundary.runId
          ? `Background run ${asyncBoundary.runId}`
          : `Background dependency job ${asyncBoundary.jobId || ""}`;
      return {
        ok: false,
        error_type: "async_boundary_active",
        terminal: true,
        should_retry: false,
        retry_hint: null,
        async_boundary: true,
        wait_for_wake: true,
        run_id: asyncBoundary.runId,
        job_id: asyncBoundary.jobId,
        message:
          `${backgroundLabel} was already started by ${asyncBoundary.toolName}. ` +
          "End this turn and wait for project-state events or a wake event; do not call more tools to poll status.",
      };
    }
    const payload = await callBackendLoggedTool(toolName, toolCallId, projectId, baseUrl, token, path, options, signal, sessionId);
    if (isRunAsyncBoundaryPayload(toolName, payload)) {
      asyncBoundary.active = true;
      asyncBoundary.toolName = toolName;
      asyncBoundary.runId = payload.run_id;
      asyncBoundary.jobId = null;
      asyncBoundary.startedCount = Number(payload.started_count || 1);
      asyncBoundary.batch = Boolean(payload.batch || (toolName === "submit_claimed_workboard_items" && asyncBoundary.startedCount > 1));
      asyncBoundary.installOnly = false;
    } else if (isDependencyJobAsyncBoundaryPayload(toolName, payload)) {
      asyncBoundary.active = true;
      asyncBoundary.toolName = toolName;
      asyncBoundary.runId = null;
      asyncBoundary.jobId = payload.job_id;
      asyncBoundary.startedCount = 0;
      asyncBoundary.batch = false;
      asyncBoundary.installOnly = true;
    }
    return payload;
  };
  const tools = [
    {
      name: "inspect_project_summary",
      label: "Inspect project summary",
      description: "Read compact project status: card ids/titles/status/steps, active runs, blockers, and asset counts. Prefer this before full project context.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "inspect_project_summary",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/inspect`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("inspect_project_summary", payload);
      },
    },
    {
      name: "get_background_workboard",
      label: "Get background workboard",
      description: "Read the backend-derived background workboard. Use this first in auto/background turns instead of reconstructing state from general inspect tools.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "get_background_workboard",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/background-workboard`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_background_workboard", payload);
      },
    },
    {
      name: "inspect_dependency_attention",
      label: "Inspect dependency attention",
      description: "Read derived dependency ATTENTION diagnostics. Call this after revise_card_plan/delete_card returns dependency_attention_check_recommended, or when checking stale/missing/outdated upstream asset chains. If it reports input_asset_outdated with current_asset_id, repair by revise_card_plan replacing the downstream inputs[].asset_id before start_card_run.",
      parameters: Type.Object({
        card_ids: Type.Optional(Type.Array(Type.String())),
        source_card_id: Type.Optional(Type.String()),
        include_recursive_downstream: Type.Optional(Type.Boolean()),
        max_issues: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "inspect_dependency_attention",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/dependency-attention/inspect`,
          {
            method: "POST",
            body: params,
          },
          signal,
          sessionId,
        );
        return toolTextResult("inspect_dependency_attention", payload);
      },
    },
    {
      name: "find_cards",
      label: "Find cards",
      description: "Find cards by query, status, step, or asset_id. Use when updating a named/selected analysis card without reading the full graph.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        status: Type.Optional(Type.String()),
        step: Type.Optional(Type.Number()),
        asset_id: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "find_cards",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/cards/find`,
          {
            method: "POST",
            body: params,
          },
          signal,
          sessionId,
        );
        return toolTextResult("find_cards", payload);
      },
    },
    {
      name: "find_assets",
      label: "Find assets",
      description: "Find materialized or planned assets by role, artifact_class, format, producer_card_id, status, or query. Use before choosing input/output asset ids.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        role: Type.Optional(Type.String()),
        artifact_class: Type.Optional(Type.String()),
        format: Type.Optional(Type.String()),
        producer_card_id: Type.Optional(Type.String()),
        status: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "find_assets",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/assets/find`,
          {
            method: "POST",
            body: params,
          },
          signal,
          sessionId,
        );
        return toolTextResult("find_assets", payload);
      },
    },
    {
      name: "get_card_detail",
      label: "Get card detail",
      description: "Read one card body with inputs, outputs, executor_context, instruction blocks, and recent runs. Use before precise revise_card_plan or annotate_card edits.",
      parameters: Type.Object({
        card_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "get_card_detail",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/cards/${encodeURIComponent(params.card_id)}/detail`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_card_detail", payload);
      },
    },
    {
      name: "get_asset_detail",
      label: "Get asset detail",
      description: "Read one asset detail/preview by exact asset_id. Use for script binding, result preview, or manifest-level diagnosis.",
      parameters: Type.Object({
        asset_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "get_asset_detail",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/assets/${encodeURIComponent(params.asset_id)}/detail`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_asset_detail", payload);
      },
    },
    {
      name: "get_project_context",
      label: "Read project context",
      description: "Read the full Blueprint project. This is large; use inspect_project_summary, find_cards, find_assets, or get_card_detail first unless full graph data is required.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "get_project_context",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/context`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_project_context", payload);
      },
    },
    {
      name: "list_data_assets",
      label: "Read data assets timeline",
      description: "Read the full data asset timeline. This is large; use find_assets first unless full timeline data is required.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "list_data_assets",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/data-assets`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("list_data_assets", payload);
      },
    },
    {
      name: "list_project_memory",
      label: "Read project memory",
      description: "Read short project memory containing only explicit user preferences and corrections. Use for plotting style, report style, recurring preferences, or avoiding previously corrected behavior. Do not use it as the source of project execution facts; use blueprint tools for that.",
      parameters: Type.Object({
        kind: Type.Optional(Type.String({ description: "user_preference or correction_memory" })),
        query: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Number({ description: "Maximum memories to return. Defaults to 5, capped by backend." })),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "list_project_memory",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/memory/list`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("list_project_memory", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "list_project_memory_failed", tool_name: "list_project_memory" });
        }
      },
    },
    {
      name: "write_project_memory",
      label: "Write project memory",
      description: "Store or update a durable project memory. Only write when the user explicitly asks you to remember a preference/default or corrects behavior to avoid. Valid kinds are user_preference and correction_memory. Keep summaries short and do not store blueprint facts.",
      parameters: Type.Object({
        memory_id: Type.Optional(Type.String()),
        kind: Type.String({ description: "user_preference or correction_memory" }),
        summary: Type.String({ description: "Short durable rule. Do not include raw chat transcripts or project execution facts." }),
        source: Type.Optional(Type.String()),
        confidence: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "write_project_memory",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/memory`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("write_project_memory", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "write_project_memory_failed", tool_name: "write_project_memory" });
        }
      },
    },
    {
      name: "create_card",
      label: "Create card",
      description: "Create a new blueprint card directly. A card is a blueprint unit. Use selected input asset ids and semantic output contracts. The backend generates card_id, output asset ids, labels, formats, and default statuses. Validation errors come back as structured repair guidance. Prefer the smallest valid step for each card; group independent cards at the same step so the workboard can batch them. When get_background_workboard reports existing ready cards under a parallel_group, align new cards with the same inputs to the same step.",
      parameters: Type.Object({
        title: Type.String(),
        step: Type.Optional(Type.Number({ description: "Parallel-execution layer. Cards with no mutual dependency should share the same step. Backend validates the lower bound derived from input assets; do not pad step beyond that bound." })),
        summary: Type.String(),
        inputs: Type.Optional(Type.Array(Type.Object({ asset_id: Type.String() }), { description: "Selected input asset ids. Use exact asset ids from find_assets or planned upstream outputs." })),
        outputs: Type.Optional(Type.Array(Type.Object({
          role: Type.String(),
          artifact_class: Type.Union([
            Type.Literal("document"),
            Type.Literal("table"),
            Type.Literal("figure"),
            Type.Literal("model"),
            Type.Literal("archive"),
            Type.Literal("binary"),
          ]),
          description: Type.Optional(Type.String()),
        }), { description: "Semantic output contracts. The backend derives labels, formats, statuses, and output asset ids." })),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "create_card",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/cards`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("create_card", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "create_card_failed", tool_name: "create_card" });
        }
      },
    },
    {
      name: "revise_card_plan",
      label: "Revise card plan",
      description: "Revise an existing card plan. Use this only for execution-relevant changes: step, selected input asset ids, or semantic outputs. It can reset the card to planned for a new run. When repairing dependency ATTENTION/input_asset_outdated, replace the downstream input asset id here, then use start_card_run. Validation errors come back as structured repair guidance. When adjusting step, prefer the smallest valid value that keeps the card in its parallel layer. Do not shift a card to a later step unless a new input dependency requires it.",
      parameters: Type.Object({
        card_id: Type.String(),
        step: Type.Optional(Type.Number({ description: "Parallel-execution layer. Prefer the smallest valid value; do not pad beyond the lower bound." })),
        inputs: Type.Optional(Type.Array(Type.Object({ asset_id: Type.String() }))),
        outputs: Type.Optional(Type.Array(Type.Object({
          role: Type.String(),
          artifact_class: Type.Union([
            Type.Literal("document"),
            Type.Literal("table"),
            Type.Literal("figure"),
            Type.Literal("model"),
            Type.Literal("archive"),
            Type.Literal("binary"),
          ]),
          description: Type.Optional(Type.String()),
        }))),
      }),
      execute: async (toolCallId, params, signal) => {
        const { card_id: cardId, ...body } = params;
        try {
          const payload = await callLoggedTool(
            "revise_card_plan",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/cards/${cardId}`,
            {
              method: "PATCH",
              body,
            },
            signal,
          sessionId,
        );
          return toolTextResult("revise_card_plan", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "revise_card_plan_failed", tool_name: "revise_card_plan" });
        }
      },
    },
    {
      name: "annotate_card",
      label: "Annotate card",
      description: "Update display-only card text without changing execution semantics. Use title/summary for direct edits, manager_review for explicit replacement, or manager_review_append to add a note without replacing existing review text. Do not use this for step, inputs, outputs, or dependency repair.",
      parameters: Type.Object({
        card_id: Type.String(),
        title: Type.Optional(Type.String()),
        summary: Type.Optional(Type.String()),
        manager_review: Type.Optional(Type.String()),
        manager_review_append: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        const { card_id: cardId, ...body } = params;
        try {
          const payload = await callLoggedTool(
            "annotate_card",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/cards/${cardId}/annotate`,
            {
              method: "POST",
              body,
            },
            signal,
          sessionId,
        );
          return toolTextResult("annotate_card", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "annotate_card_failed", tool_name: "annotate_card" });
        }
      },
    },
    {
      name: "delete_card",
      label: "Delete card",
      description: "Cancel a blueprint card by exact card_id. This marks the card as cancelled instead of deleting historical records. Use find_cards first if the exact card_id is uncertain.",
      parameters: Type.Object({
        card_id: Type.String(),
        reason: Type.Optional(Type.String()),
        message: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        const { card_id: cardId, ...body } = params;
        try {
          const payload = await callLoggedTool(
            "delete_card",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/cards/${cardId}`,
            {
              method: "DELETE",
              body,
            },
            signal,
          sessionId,
        );
          return toolTextResult("delete_card", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "delete_card_failed", tool_name: "delete_card" });
        }
      },
    },
    {
      name: "configure_card_execution",
      label: "Configure card execution",
      description: "Update selected skills, MCP servers, Python/R runtime bindings, and instruction blocks for one or more cards. Use this only when a card needs non-default runtimes or library/tool attachments, or to persist script_preference into a card's executor context. It is not a permission editor and does not control network, shell, env vars, working directory, or free-form instruction patches.",
      parameters: Type.Object({
        card_ids: Type.Optional(Type.Array(Type.String())),
        skills: Type.Optional(Type.Array(Type.String())),
        mcp_servers: Type.Optional(Type.Array(Type.String())),
        runtime_bindings: Type.Optional(
          Type.Object({
            conda_env: Type.Optional(Type.String()),
            r_env: Type.Optional(Type.String()),
          }),
        ),
        instruction_blocks: Type.Optional(
          Type.Array(Type.String(), {
            description: "Free-form planning hints the executor should read. Use this to persist selected_context.script_preference into the card. Keep entries short and non-binding.",
          }),
        ),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "configure_card_execution",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/card-execution`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("configure_card_execution", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "configure_card_execution_failed", tool_name: "configure_card_execution" });
        }
      },
    },
    {
      name: "install_runtime_dependencies",
      label: "Install runtime dependencies",
      description: "Start a background job that installs explicitly named Python or R packages into an already selected non-system runtime. Use only for clear package lists. When fixing a specific card/run dependency issue, include source.card_id and source.run_id when known. Do not choose or pass an installer; backend chooses the conda-family solver by default and only executes registry fallback when the resolver approves one safe family for the whole request. After a successful start, report the job_id and stop this turn; do not poll status with get_runtime_dependency_install_status in the same turn.",
      parameters: Type.Object({
        ecosystem: Type.String({ description: "python or R. When selected_context.script_preference is prefer_r, default ecosystem to R unless the task clearly needs a Python-only package; mirror for prefer_python. Do not invent a third ecosystem value." }),
        runtime: Type.String({ description: "Selected non-system runtime name, such as omicverse, rnaseq, or R_env. Do not use __system__." }),
        packages: Type.Array(Type.String({ description: "Package names or simple Python version specs." })),
        timeout_seconds: Type.Optional(Type.Number()),
        source: Type.Optional(
          Type.Object({
            card_id: Type.Optional(Type.String()),
            run_id: Type.Optional(Type.String()),
          }),
        ),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "install_runtime_dependencies",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runtime-dependencies/install`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult(
            "install_runtime_dependencies",
            payload,
            isDependencyJobAsyncBoundaryPayload("install_runtime_dependencies", payload),
          );
        } catch (error) {
          return toolErrorResult(error, { error_type: "install_runtime_dependencies_failed", tool_name: "install_runtime_dependencies" });
        }
      },
    },
    {
      name: "resolve_runtime_dependencies",
      label: "Resolve runtime dependencies",
      description: "Plan-only counterpart of install_runtime_dependencies. Returns a resolver plan (status, installable, blocked, fallback_actions) for the same request shape, without creating a background job. Use this to inspect what would happen before mutating runtime state. If status is not \"fully_installable\", do NOT retry install_runtime_dependencies with the original payload; ask the user how to proceed or submit a narrower explicit request for only the installable subset. fallback_available_but_ambiguous means the backend still could not choose one safe registry family after applying its built-in preferences; do not guess pip/cran/bioconductor.",
      parameters: Type.Object({
        ecosystem: Type.String({ description: "python or R. When selected_context.script_preference is prefer_r, default ecosystem to R unless the task clearly needs a Python-only package; mirror for prefer_python. Do not invent a third ecosystem value." }),
        runtime: Type.String({ description: "Selected non-system runtime name." }),
        packages: Type.Array(Type.String({ description: "Package names or simple Python version specs." })),
        source: Type.Optional(
          Type.Object({
            card_id: Type.Optional(Type.String()),
            run_id: Type.Optional(Type.String()),
          }),
        ),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "resolve_runtime_dependencies",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runtime-dependencies/resolve`,
            {
              method: "POST",
              body: params,
            },
            signal,
            sessionId,
          );
          return toolTextResult("resolve_runtime_dependencies", payload);
        } catch (error) {
          return toolErrorResult(error, {
            error_type: "resolve_runtime_dependencies_failed",
            tool_name: "resolve_runtime_dependencies",
          });
        }
      },
    },
    {
      name: "get_runtime_dependency_install_status",
      label: "Get runtime dependency install status",
      description: "Check whether a background runtime dependency installation job has finished, failed, or is still running. Use for explicit user checks, recovery, or later wake turns; do not poll a job that was just started in the same turn.",
      parameters: Type.Object({
        job_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "get_runtime_dependency_install_status",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runtime-dependencies/jobs/${encodeURIComponent(params.job_id)}`,
            {
              method: "GET",
            },
            signal,
          sessionId,
        );
          return toolTextResult("get_runtime_dependency_install_status", payload);
        } catch (error) {
          return toolErrorResult(error, {
            error_type: "get_runtime_dependency_install_status_failed",
            tool_name: "get_runtime_dependency_install_status",
          });
        }
      },
    },
    {
      name: "promote_workboard_item_to_todo",
      label: "Promote workboard item",
      description: "Promote a ready_to_start workboard item into backend-verified todo state. Use item ids from get_background_workboard.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "promote_workboard_item_to_todo",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/promote`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("promote_workboard_item_to_todo", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "promote_workboard_item_to_todo_failed", tool_name: "promote_workboard_item_to_todo" });
        }
      },
    },
    {
      name: "claim_workboard_item",
      label: "Claim workboard item",
      description: "Claim one workboard item for the current manager session before consuming it.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "claim_workboard_item",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/claim`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("claim_workboard_item", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "claim_workboard_item_failed", tool_name: "claim_workboard_item" });
        }
      },
    },
    {
      name: "complete_workboard_item",
      label: "Complete workboard item",
      description: "Mark one claimed or handled workboard item as done so it does not keep waking auto turns.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "complete_workboard_item",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/complete`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("complete_workboard_item", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "complete_workboard_item_failed", tool_name: "complete_workboard_item" });
        }
      },
    },
    {
      name: "skip_workboard_item",
      label: "Skip workboard item",
      description: "Skip a workboard todo item — the only explicit way to exit todo fuel. Marks the item as done and sets fuel_consumed_at.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "skip_workboard_item",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/skip`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("skip_workboard_item", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "skip_workboard_item_failed", tool_name: "skip_workboard_item" });
        }
      },
    },
    {
      name: "defer_workboard_item",
      label: "Defer workboard item",
      description: "Defer a workboard item to a later turn when another action batch should happen first.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "defer_workboard_item",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/defer`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("defer_workboard_item", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "defer_workboard_item_failed", tool_name: "defer_workboard_item" });
        }
      },
    },
    {
      name: "block_workboard_item_for_user",
      label: "Block workboard item",
      description: "Mark a workboard item as blocked_for_user when it needs user input or an external change.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "block_workboard_item_for_user",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/block`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("block_workboard_item_for_user", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "block_workboard_item_for_user_failed", tool_name: "block_workboard_item_for_user" });
        }
      },
    },
    {
      name: "reopen_workboard_item",
      label: "Reopen workboard item",
      description: "Reopen a deferred or blocked workboard item after inputs or state changed.",
      parameters: Type.Object({
        item_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "reopen_workboard_item",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/reopen`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("reopen_workboard_item", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "reopen_workboard_item_failed", tool_name: "reopen_workboard_item" });
        }
      },
    },
    {
      name: "submit_claimed_workboard_items",
      label: "Submit claimed workboard items",
      description: "Submit claimed todo workboard items as one backend-owned background run batch. Use this for frontier or multi-card execution after get_background_workboard -> promote -> claim. If any start succeeds, stop the turn and wait for events/wake.",
      parameters: Type.Object({
        todo_item_ids: Type.Array(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "submit_claimed_workboard_items",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/background-workboard/submit-claimed`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("submit_claimed_workboard_items", payload, isRunAsyncBoundaryPayload("submit_claimed_workboard_items", payload));
        } catch (error) {
          return toolErrorResult(error, { error_type: "submit_claimed_workboard_items_failed", tool_name: "submit_claimed_workboard_items" });
        }
      },
    },
    {
      name: "finish_auto_episode",
      label: "Finish auto episode",
      description: "Confirm that the current auto episode is complete and transition to finished state. Valid only when the auto state is 'complete'. Provide a complete_message summarizing the objective, completion status, outputs, and remaining caveats.",
      parameters: Type.Object({
        complete_message: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "finish_auto_episode",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/finish-auto-episode`,
            { method: "POST", body: params },
            signal,
            sessionId,
          );
          return toolTextResult("finish_auto_episode", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "finish_auto_episode_failed", tool_name: "finish_auto_episode" });
        }
      },
    },
    {
      name: "start_card_run",
      label: "Start card run",
      description: "Start executing one explicit card as background work. Use the card's saved execution configuration when present; otherwise the backend uses system defaults. For frontier or multi-card execution, prefer get_background_workboard -> promote -> claim -> submit. Use configure_card_execution only when the card needs non-default Python/R runtime bindings or attached skills/MCP servers. If successful, report the run_id and stop the turn; do not poll card status while waiting. If can_start is false, inspect block_reasons and fix the blocker before retrying.",
      parameters: Type.Object({
        card_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "start_card_run",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runs/start`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("start_card_run", payload, isRunAsyncBoundaryPayload("start_card_run", payload));
        } catch (error) {
          return toolErrorResult(error, { error_type: "start_card_run_failed", tool_name: "start_card_run" });
        }
      },
    },
    {
      name: "stop_card_run",
      label: "Stop card run",
      description: "Stop the active run or runs for a card. The backend resolves the active runs by card_id and stops all of them if multiple active runs exist unexpectedly.",
      parameters: Type.Object({
        card_id: Type.String(),
        reason: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "stop_card_run",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runs/stop`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("stop_card_run", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "stop_card_run_failed", tool_name: "stop_card_run" });
        }
      },
    },
    {
      name: "rerun_card",
      label: "Rerun card",
      description: "Start a fresh background rerun for a card after a previous run finished or failed. It reuses the card's saved execution configuration when present; otherwise the backend uses system defaults. It also reuses the current saved inputs[].asset_id values. rerun_card is a strict retry, not a dependency repair tool: if inputs are stale or outdated, revise the card plan first and then use start_card_run. If the card is not already planned, rerun_card may reset it to planned before launching the new run.",
      parameters: Type.Object({
        card_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "rerun_card",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runs/rerun`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("rerun_card", payload, isRunAsyncBoundaryPayload("rerun_card", payload));
        } catch (error) {
          return toolErrorResult(error, { error_type: "rerun_card_failed", tool_name: "rerun_card" });
        }
      },
    },
    {
      name: "review_card_run",
      label: "Review card run",
      description: "Finalize the latest run for a card. This is a finalize/accept attempt only; the backend decides the final accepted result based on manifest validation and graph consistency checks. Do not use this to reject runs.",
      parameters: Type.Object({
        card_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "review_card_run",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runs/review`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("review_card_run", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "review_card_run_failed", tool_name: "review_card_run" });
        }
      },
    },
    {
      name: "cleanup_run_history",
      label: "Clean run history",
      description: "Remove old finished run execution files, caches, transient state, and candidate artifacts. Use after failed/cancelled/rejected runs accumulate or before reruns. Defaults preserve the latest run per card and runs that own valid accepted assets.",
      parameters: Type.Object({
        run_id: Type.Optional(Type.String({ description: "Clean one specific run. If omitted, cleanup can target card_id or project-wide old runs." })),
        card_id: Type.Optional(Type.String({ description: "Clean old runs for one card." })),
        statuses: Type.Optional(Type.Array(Type.String({ description: "Run statuses to consider. Defaults to failed, cancelled, reviewed." }))),
        keep_latest_per_card: Type.Optional(Type.Boolean({ description: "Default true. Keep the newest run for each card unless run_id is specified." })),
        include_valid_assets: Type.Optional(Type.Boolean({ description: "Default false. Leave runs that own valid accepted assets untouched." })),
        dry_run: Type.Optional(Type.Boolean({ description: "Preview what would be cleaned without deleting files." })),
        reason: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "cleanup_run_history",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/runs/cleanup-history`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("cleanup_run_history", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "cleanup_run_history_failed", tool_name: "cleanup_run_history" });
        }
      },
    },
    {
      name: "search_card_templates",
      label: "Search card templates",
      description: "Search the manager-only local card template library before designing a repeated workflow from scratch.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
        card_type: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "search_card_templates",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/card-templates/search`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("search_card_templates", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "search_card_templates_failed", tool_name: "search_card_templates" });
        }
      },
    },
    {
      name: "save_card_template",
      label: "Save card template",
      description: "Save a stable accepted/reviewer-passed card into the manager-only card template library for later reuse.",
      parameters: Type.Object({
        card_id: Type.String(),
        title: Type.Optional(Type.String()),
        summary: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "save_card_template",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/card-templates`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("save_card_template", payload);
        } catch (error) {
          return toolErrorResult(error, { error_type: "save_card_template_failed", tool_name: "save_card_template" });
        }
      },
    },
    {
      name: "instantiate_card_template",
      label: "Instantiate card template",
      description: "Create a new card from a saved template. If the template requires script assets, ask the user which project script assets to bind before calling this tool.",
      parameters: Type.Object({
        template_id: Type.String(),
        title: Type.Optional(Type.String()),
        step: Type.Optional(Type.Number()),
        input_bindings: Type.Optional(Type.Array(Type.Object({ asset_id: Type.String() }))),
        script_asset_bindings: Type.Optional(
          Type.Array(
            Type.Object({
              requirement_id: Type.String(),
              asset_id: Type.String(),
            }),
          ),
        ),
      }),
      execute: async (toolCallId, params, signal) => {
        try {
          const payload = await callLoggedTool(
            "instantiate_card_template",
            toolCallId,
            projectId,
            baseUrl,
            token,
            `/internal/manager-tools/projects/${projectId}/card-templates/instantiate`,
            {
              method: "POST",
              body: params,
            },
            signal,
          sessionId,
        );
          return toolTextResult("instantiate_card_template", { ok: true, ...payload });
        } catch (error) {
          return toolErrorResult(error, { error_type: "instantiate_card_template_failed", tool_name: "instantiate_card_template" });
        }
      },
    },
    {
      name: "read_result_asset",
      label: "Read result asset",
      description: "Read a whitelisted result asset detail/preview by exact asset_id.",
      parameters: Type.Object({
        asset_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "read_result_asset",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/assets/${params.asset_id}`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("read_result_asset", payload);
      },
    },
    {
      name: "list_skill_library",
      label: "List skill library",
      description: "Read installed skill ids and names only. Use this for cheap discovery before attaching an obvious skill id to a card.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "list_skill_library",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/skill-library`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("list_skill_library", payload);
      },
    },
    {
      name: "search_skill_library",
      label: "Search skill library",
      description: "Search the skill library and return id/name-only matches. Use this only when a card clearly needs reusable skills.",
      parameters: Type.Object({
        query: Type.String(),
        runtime: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
        top_k: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "search_skill_library",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/skill-library/search`,
          {
            method: "POST",
            body: params,
          },
          signal,
          sessionId,
        );
        return toolTextResult("search_skill_library", payload);
      },
    },
    {
      name: "get_skill_library_item",
      label: "Get skill details",
      description: "Read one skill library item with summary and compatibility details. Use only when id/name-only search is ambiguous.",
      parameters: Type.Object({
        skill_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "get_skill_library_item",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/skill-library/${encodeURIComponent(params.skill_id)}`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_skill_library_item", payload);
      },
    },
    {
      name: "list_mcp_library",
      label: "List MCP library",
      description: "Read MCP server ids and names only. Use this for cheap discovery before attaching an obvious MCP id to a card.",
      parameters: Type.Object({}),
      execute: async (toolCallId, _params, signal) => {
        const payload = await callLoggedTool(
          "list_mcp_library",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/mcp-library`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("list_mcp_library", payload);
      },
    },
    {
      name: "search_mcp_library",
      label: "Search MCP library",
      description: "Search the MCP library and return id/name-only matches. Use only when a card clearly needs runtime tool providers beyond plain prompts.",
      parameters: Type.Object({
        query: Type.String(),
        runtime: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
        top_k: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "search_mcp_library",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/mcp-library/search`,
          {
            method: "POST",
            body: params,
          },
          signal,
          sessionId,
        );
        return toolTextResult("search_mcp_library", payload);
      },
    },
    {
      name: "get_mcp_library_item",
      label: "Get MCP details",
      description: "Read one MCP library item with summary, supported runtimes, compatibility notes, and launch hint. Use only when id/name-only search is ambiguous.",
      parameters: Type.Object({
        entry_id: Type.String(),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "get_mcp_library_item",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/mcp-library/${encodeURIComponent(params.entry_id)}`,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_mcp_library_item", payload);
      },
    },
    {
      name: "search_card_packages",
      label: "Search card packages",
      description:
        "Search the portable card package registry. Returns id/name/summary/tags/compatibility matches. " +
        "Use this first to discover packages before calling get_card_package_detail.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        runtime: Type.Optional(Type.String()),
        tags: Type.Optional(Type.Array(Type.String())),
        top_k: Type.Optional(Type.Number()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "search_card_packages",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/packages/search`,
          { q: params.query, runtime: params.runtime, tags: params.tags, top_k: params.top_k },
          signal,
          sessionId,
        );
        return toolTextResult("search_card_packages", payload);
      },
    },
    {
      name: "get_card_package_detail",
      label: "Get card package detail",
      description:
        "Read one portable card package with full manifest and bundle file listing. " +
        "Use this after search_card_packages when a package id is ambiguous or you need to confirm compatibility before import.",
      parameters: Type.Object({
        package_id: Type.String(),
        version: Type.Optional(Type.String()),
      }),
      execute: async (toolCallId, params, signal) => {
        let path = `/internal/manager-tools/projects/${projectId}/packages/${encodeURIComponent(params.package_id)}`;
        if (params.version) path += `?version=${encodeURIComponent(params.version)}`;
        const payload = await callLoggedTool(
          "get_card_package_detail",
          toolCallId,
          projectId,
          baseUrl,
          token,
          path,
          {},
          signal,
          sessionId,
        );
        return toolTextResult("get_card_package_detail", payload);
      },
    },
    {
      name: "import_card_package",
      label: "Import card package",
      description:
        "Import a portable card package from a local directory or zip archive path on the server. " +
        "This is a mutation — it stores the package in the local registry. " +
        "Call this only after the package has been reviewed (via search/detail).",
      parameters: Type.Object({
        source_path: Type.String(),
        overwrite: Type.Optional(Type.Boolean()),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "import_card_package",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/packages/import`,
          { source_path: params.source_path, overwrite: params.overwrite },
          signal,
          sessionId,
        );
        return toolTextResult("import_card_package", payload);
      },
    },
    {
      name: "instantiate_card_package",
      label: "Instantiate card package",
      description:
        "Create a card from an imported package. Requires package_id. " +
        "Optionally bind input assets, set parameter overrides, or pin a runtime override. " +
        "This is a mutation — it creates a new card in the project. " +
        "Always search/detail first, then instantiate with explicit bindings.",
      parameters: Type.Object({
        package_id: Type.String(),
        version: Type.Optional(Type.String()),
        input_bindings: Type.Optional(Type.Record(Type.String(), Type.String())),
        parameter_bindings: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
        runtime_override: Type.Optional(Type.Record(Type.String(), Type.String())),
      }),
      execute: async (toolCallId, params, signal) => {
        const payload = await callLoggedTool(
          "instantiate_card_package",
          toolCallId,
          projectId,
          baseUrl,
          token,
          `/internal/manager-tools/projects/${projectId}/packages/${encodeURIComponent(params.package_id)}/instantiate`,
          {
            package_id: params.package_id,
            version: params.version,
            input_bindings: params.input_bindings,
            parameter_bindings: params.parameter_bindings,
            runtime_override: params.runtime_override,
          },
          signal,
          sessionId,
        );
        return toolTextResult("instantiate_card_package", payload);
      },
    },
  ];
  const mutatingTools = new Set([
    "create_card",
    "revise_card_plan",
    "annotate_card",
    "delete_card",
    "configure_card_execution",
    "install_runtime_dependencies",
    "promote_workboard_item_to_todo",
    "claim_workboard_item",
    "complete_workboard_item",
    "skip_workboard_item",
    "defer_workboard_item",
    "block_workboard_item_for_user",
    "reopen_workboard_item",
    "submit_claimed_workboard_items",
    "finish_auto_episode",
    "start_card_run",
    "stop_card_run",
    "rerun_card",
    "review_card_run",
    "cleanup_run_history",
    "save_card_template",
    "instantiate_card_template",
    "import_card_package",
    "instantiate_card_package",
    "write_project_memory",
  ]);
  let visibleTools = btwMode ? tools.filter((tool) => !mutatingTools.has(tool.name)) : tools;
  if (runtimeConfig.websearchEnabled && runtimeConfig.tavilyApiKey) {
    visibleTools.push(
      {
        name: "web_search",
        label: "Search the web",
        description: "Search current public web information via Tavily. Use for current docs, recent behavior, or external verification.",
        parameters: Type.Object({
          query: Type.String(),
          search_depth: Type.Optional(Type.String({ description: "basic or advanced" })),
          max_results: Type.Optional(Type.Number()),
          include_domains: Type.Optional(Type.Array(Type.String())),
          exclude_domains: Type.Optional(Type.Array(Type.String())),
        }),
        execute: async (_toolCallId, params, signal) => {
          const payload = await callTavily(
            "/search",
            {
              query: params.query,
              search_depth: params.search_depth || "basic",
              max_results: params.max_results || 5,
              include_domains: params.include_domains,
              exclude_domains: params.exclude_domains,
              include_answer: true,
            },
            signal,
            runtimeConfig,
          );
          return toolTextResult("web_search", payload);
        },
      },
      {
        name: "web_extract",
        label: "Extract web page content",
        description: "Extract readable markdown or text from specific URLs via Tavily.",
        parameters: Type.Object({
          urls: Type.Array(Type.String()),
          extract_depth: Type.Optional(Type.String({ description: "basic or advanced" })),
          format: Type.Optional(Type.String({ description: "markdown or text" })),
        }),
        execute: async (_toolCallId, params, signal) => {
          const payload = await callTavily(
            "/extract",
            {
              urls: params.urls,
              extract_depth: params.extract_depth || "basic",
              format: params.format || "markdown",
            },
            signal,
            runtimeConfig,
          );
          return toolTextResult("web_extract", payload);
        },
      },
    );
  }
  return {
    tools: visibleTools,
    getTurnControl: () => ({
      async_boundary: { ...asyncBoundary },
    }),
  };
}

function extractText(message) {
  if (!message || !Array.isArray(message.content)) {
    return "";
  }
  return message.content
    .filter((item) => item.type === "text" && typeof item.text === "string")
    .map((item) => item.text)
    .join("");
}

function toolStatusLabel(toolName) {
  return TOOL_STATUS_LABELS[toolName] || {
    active: `正在使用 ${toolName}`,
    done: `已完成 ${toolName}`,
  };
}

function collectThinking(thinkingBlocks) {
  return Array.from(thinkingBlocks.values())
    .map((value) => value?.trim())
    .filter(Boolean)
    .join("\n\n");
}

function normalizeHistory(messages) {
  if (!Array.isArray(messages)) {
    return [];
  }
  return messages
    .filter((message) => message && (message.role === "user" || message.role === "manager") && typeof message.content === "string")
    .map((message) => ({
      role: message.role === "manager" ? "assistant" : "user",
      content: [{ type: "text", text: message.content }],
      timestamp: Date.now(),
    }));
}

function compactTimelineItem(message) {
  if (!Array.isArray(message?.timeline)) {
    return null;
  }
  return message.timeline.find((item) => item?.kind === "compact" && typeof item?.content === "string" && item.content.trim());
}

const CONTEXT_FALLBACK_MESSAGE_LIMIT = 50;

function messageTextForContext(message) {
  if (!message || typeof message !== "object") {
    return "";
  }
  const compactionItem = compactTimelineItem(message);
  if (compactionItem?.content) {
    return compactionItem.content.trim();
  }
  if (typeof message.content === "string" && message.content.trim()) {
    return message.content.trim();
  }
  return "";
}

function buildSessionEntries(sessionMessages = [], runtimeConfig = resolveManagerConfig()) {
  const entries = [];
  let parentId = null;
  const baseTime = Date.now();
  for (let index = 0; index < sessionMessages.length; index += 1) {
    const message = sessionMessages[index];
    if (!message || (message.role !== "user" && message.role !== "manager")) {
      continue;
    }
    const compactionItem = compactTimelineItem(message);
    const timestamp = new Date(baseTime + index).toISOString();
    if (compactionItem?.content) {
      const entryId = compactionItem.id || message.id || `compact_${index}`;
      entries.push({
        type: "compaction",
        id: entryId,
        parentId,
        timestamp,
        summary: compactionItem.content.trim(),
        firstKeptEntryId: compactionItem.first_kept_message_id || "root",
        tokensBefore: Number(compactionItem.tokens_before) || 0,
        details: {
          tokens_after: Number(compactionItem.tokens_after) || undefined,
          duration_ms: Number(compactionItem.duration_ms) || undefined,
          provider: compactionItem.provider || undefined,
          model: compactionItem.model || undefined,
        },
        fromHook: true,
      });
      parentId = entryId;
      continue;
    }
    const text = messageTextForContext(message);
    if (!text) {
      continue;
    }
    const entryId = message.id || `message_${index}`;
    entries.push({
      type: "message",
      id: entryId,
      parentId,
      timestamp,
      message: {
        role: message.role === "manager" ? "assistant" : "user",
        content: [{ type: "text", text }],
        timestamp: baseTime + index,
        provider: runtimeConfig.provider,
        model: runtimeConfig.model,
      },
    });
    parentId = entryId;
  }
  return entries;
}

function compactionSettings() {
  return {
    ...DEFAULT_COMPACTION_SETTINGS,
    enabled: MANAGER_COMPACTION_ENABLED,
    keepRecentTokens: MANAGER_COMPACTION_KEEP_RECENT_TOKENS,
    reserveTokens: MANAGER_COMPACTION_RESERVE_TOKENS,
  };
}

function currentContextWindow(model) {
  if (MANAGER_CONTEXT_WINDOW_TOKENS > 0) {
    return MANAGER_CONTEXT_WINDOW_TOKENS;
  }
  const fromModel = Number.isFinite(Number(model?.contextWindow)) ? Number(model.contextWindow) : 0;
  return fromModel > 0 ? fromModel : 0;
}

async function maybeCompactSessionHistory({
  sessionMessages,
  model,
  thinkingEffort,
  emitEvent,
  signal,
  auto = true,
  force = false,
  runtimeConfig = resolveManagerConfig(),
}) {
  const entries = buildSessionEntries(sessionMessages, runtimeConfig);
  const context = buildSessionContext(entries);
  const messages = context.messages;
  if (!messages.length) {
    return { compacted: false, contextMessages: messages };
  }
  const settings = compactionSettings();
  if (!settings.enabled) {
    return { compacted: false, contextMessages: messages };
  }
  const estimate = estimateContextTokens(messages);
  const contextWindow = currentContextWindow(model);
  const threshold = contextWindow - settings.reserveTokens;
  if (!force && (estimate.tokens <= 0 || estimate.tokens < threshold)) {
    return { compacted: false, contextMessages: messages };
  }
  const preparationResult = prepareCompaction(entries, settings);
  if (!preparationResult.ok) {
    throw preparationResult.error;
  }
  const preparation = preparationResult.value;
  if (!preparation) {
    return { compacted: false, contextMessages: messages };
  }
  const compactId = `compact_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  const startedAt = Date.now();
  emitEvent?.({
    type: "compact_start",
    compact_id: compactId,
    auto,
  });
  const compactResult = await compact(
    preparation,
    model,
    runtimeConfig.apiKey,
    undefined,
    undefined,
    signal,
    mapThinkingLevel(thinkingEffort, model),
  );
  if (!compactResult.ok) {
    emitEvent?.({
      type: "compact_error",
      compact_id: compactId,
      message: compactResult.error.message,
      auto,
    });
    throw compactResult.error;
  }
  const result = compactResult.value;
  const compactionEntry = {
    type: "compaction",
    id: compactId,
    parentId: entries.at(-1)?.id ?? null,
    timestamp: new Date().toISOString(),
    summary: result.summary,
    firstKeptEntryId: result.firstKeptEntryId,
    tokensBefore: result.tokensBefore,
    details: result.details,
    fromHook: auto,
  };
  const compactedMessages = buildSessionContext([...entries, compactionEntry]).messages;
  const tokensAfter = estimateContextTokens(compactedMessages).tokens;
  const durationMs = Date.now() - startedAt;
  emitEvent?.({
    type: "compact_end",
    compact_id: compactId,
    content: result.summary,
    duration_ms: durationMs,
    tokens_before: result.tokensBefore,
    tokens_after: tokensAfter,
    first_kept_message_id: result.firstKeptEntryId,
    provider: runtimeConfig.provider,
    model: runtimeConfig.model,
    auto,
  });
  return {
    compacted: true,
    compactId,
    contextMessages: compactedMessages,
    summary: result.summary,
    firstKeptMessageId: result.firstKeptEntryId,
    tokensBefore: result.tokensBefore,
    tokensAfter,
    durationMs,
    provider: runtimeConfig.provider,
    model: runtimeConfig.model,
  };
}

async function callTavily(path, payload, signal, runtimeConfig = resolveManagerConfig()) {
  if (!runtimeConfig.websearchEnabled || !runtimeConfig.tavilyApiKey) {
    return {
      ok: false,
      disabled: true,
      message: "Web search is disabled. Set MANAGER_WEBSEARCH_ENABLED=true and configure TAVILY_API_KEY.",
    };
  }
  const response = await fetch(`${runtimeConfig.tavilyBaseUrl}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${runtimeConfig.tavilyApiKey}`,
    },
    body: JSON.stringify(payload),
    signal,
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!response.ok) {
    throw new Error(typeof data?.detail === "string" ? data.detail : `Tavily request failed with HTTP ${response.status}`);
  }
  return data;
}

function createRunId() {
  return `mgr_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeTokenUsage(usage, model) {
  if (!usage || typeof usage !== "object") {
    return null;
  }
  const numberOrZero = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);
  const inputTokens = numberOrZero(usage.input);
  const outputTokens = numberOrZero(usage.output);
  const cacheReadTokens = numberOrZero(usage.cacheRead);
  const cacheWriteTokens = numberOrZero(usage.cacheWrite);
  const totalTokens =
    numberOrZero(usage.totalTokens) || inputTokens + outputTokens + cacheReadTokens + cacheWriteTokens;
  if (totalTokens <= 0) {
    return null;
  }
  return {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    cache_read_tokens: cacheReadTokens,
    cache_write_tokens: cacheWriteTokens,
    total_tokens: totalTokens,
    context_window_tokens: currentContextWindow(model) || null,
    max_output_tokens: Number.isFinite(Number(model?.maxTokens)) ? Number(model.maxTokens) : null,
  };
}

function mapThinkingLevel(thinkingEffort, model) {
  // DeepSeek V4 Pro only exposes off/high/xhigh in pi-ai. Map the UI's
  // low/medium/high controls onto distinct model-supported levels so the
  // selector produces an actual behavioral difference.
  const requestedLevel =
    thinkingEffort === "low"
      ? "off"
      : thinkingEffort === "high"
        ? "xhigh"
        : "high";
  return clampThinkingLevel(model, requestedLevel);
}

async function runManagerChat(payload, emitEvent = null, externalAbortSignal = null) {
  const runId = createRunId();
  const runStartedAt = Date.now();
  const projectId = payload.project_id;
  const shouldSettleAutoTurn = payload.auto_mode?.enabled && payload.session_id && !payload.auto_mode?.btw_mode;
  async function settleAutoTurnBestEffort(asyncBoundary = false) {
    if (!shouldSettleAutoTurn) return;
    try {
      await notifyAutoTurnSettled(
        payload.backend_api_base_url,
        payload.internal_tool_token,
        payload.project_id,
        payload.session_id,
        asyncBoundary,
        AbortSignal.timeout(10000),
      );
    } catch (_) { /* best effort — latch cleared on next restart regardless */ }
  }
  const runtimeConfig = resolveManagerConfig(payload);
  if (!runtimeConfig.apiKey) {
    await settleAutoTurnBestEffort();
    throw new Error("MANAGER_AGENT_API_KEY or BLUEPRINT_DEEPSEEK_API_KEY is not configured.");
  }
  const model = resolveModel(runtimeConfig);
  if (!model) {
    await settleAutoTurnBestEffort();
    throw new Error(`Manager model not found: provider=${runtimeConfig.provider}, model=${runtimeConfig.model}`);
  }
  const events = [];
  let finalAssistantMessage = null;
  let finalTokenUsage = null;
  let streamedText = "";
  const thinkingBlocks = new Map();
  const thinkingStartedAt = new Map();
  const completedThinkingBlocks = new Set();
  let assistantTurnIndex = -1;
  let currentAssistantTurnIndex = -1;
  let lastEmitAt = Date.now();
  let lastAgentEventAt = Date.now();
  let lastAgentEventType = "run_start";
  logManagerEvent("run_start", {
    run_id: runId,
    project_id: projectId,
    model: runtimeConfig.model,
    provider: runtimeConfig.provider,
    thinking_effort: payload.thinking_effort,
    message_chars: typeof payload.message === "string" ? payload.message.length : null,
    history_messages: Array.isArray(payload.messages) ? payload.messages.length : 0,
  });
  const emit = (event) => {
    lastEmitAt = Date.now();
    emitEvent?.(event);
  };
  const modelContext = buildSessionContext(buildSessionEntries(payload.session_messages || [], runtimeConfig));
  let initialMessages = modelContext.messages.length ? modelContext.messages : normalizeHistory(payload.messages);
  if (payload.session_messages?.length) {
    try {
      const compaction = await maybeCompactSessionHistory({
        sessionMessages: payload.session_messages,
        model,
        thinkingEffort: payload.thinking_effort,
        emitEvent: emit,
        signal: externalAbortSignal,
        auto: true,
        runtimeConfig,
      });
      initialMessages = compaction.contextMessages;
    } catch (compactionError) {
      await settleAutoTurnBestEffort();
      throw compactionError;
    }
  }
  const toolControl = createTools(payload, runtimeConfig);
  const agent = new Agent({
    initialState: {
      systemPrompt: buildSystemPrompt(runtimeConfig),
      model,
      thinkingLevel: mapThinkingLevel(payload.thinking_effort, model),
      tools: toolControl.tools,
      messages: initialMessages,
    },
    getApiKey: () => runtimeConfig.apiKey,
    toolExecution: "sequential",
    transport: "auto",
    timeoutMs: TIMEOUT_MS,
    maxRetries: PROVIDER_MAX_RETRIES,
    maxRetryDelayMs: PROVIDER_MAX_RETRY_DELAY_MS,
    transformContext: async (messages) =>
      MANAGER_COMPACTION_ENABLED ? messages : messages.slice(-CONTEXT_FALLBACK_MESSAGE_LIMIT),
  });
  agent.subscribe((event) => {
    lastAgentEventAt = Date.now();
    lastAgentEventType = event.type;
    events.push(event);
    if (event.type === "message_start" && event.message?.role === "assistant") {
      assistantTurnIndex += 1;
      currentAssistantTurnIndex = assistantTurnIndex;
    }
    if (event.type === "message_update" && event.assistantMessageEvent) {
      const assistantEvent = event.assistantMessageEvent;
      const turnIndex = currentAssistantTurnIndex >= 0 ? currentAssistantTurnIndex : 0;
      const blockKey = `${turnIndex}:${assistantEvent.contentIndex}`;
      if (assistantEvent.type === "thinking_start") {
        if (!thinkingBlocks.has(blockKey)) {
          thinkingBlocks.set(blockKey, "");
        }
        if (!thinkingStartedAt.has(blockKey)) {
          thinkingStartedAt.set(blockKey, Date.now());
        }
        emit({
          type: "thinking_start",
          content_index: assistantEvent.contentIndex,
          assistant_turn_index: turnIndex,
          started_at: thinkingStartedAt.get(blockKey),
        });
      } else if (assistantEvent.type === "thinking_delta") {
        const nextThinking = `${thinkingBlocks.get(blockKey) || ""}${assistantEvent.delta || ""}`;
        thinkingBlocks.set(blockKey, nextThinking);
        emit({
          type: "thinking_delta",
          delta: assistantEvent.delta || "",
          content_index: assistantEvent.contentIndex,
          assistant_turn_index: turnIndex,
        });
      } else if (assistantEvent.type === "thinking_end") {
        const finalizedThinking =
          typeof assistantEvent.content === "string"
            ? assistantEvent.content
            : thinkingBlocks.get(blockKey) || "";
        thinkingBlocks.set(blockKey, finalizedThinking);
        completedThinkingBlocks.add(blockKey);
        const endedAt = Date.now();
        emit({
          type: "thinking_end",
          content: finalizedThinking,
          content_index: assistantEvent.contentIndex,
          assistant_turn_index: turnIndex,
          started_at: thinkingStartedAt.get(blockKey),
          ended_at: endedAt,
        });
      } else if (assistantEvent.type === "text_delta") {
        const delta = assistantEvent.delta || "";
        streamedText += delta;
        emit({
          type: "text_delta",
          delta,
          content_index: assistantEvent.contentIndex,
          assistant_turn_index: turnIndex,
        });
      }
    }
    if (event.type === "tool_execution_start") {
      const label = toolStatusLabel(event.toolName);
      logManagerEvent("tool_execution_start", {
        run_id: runId,
        project_id: projectId,
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
      });
      emit({
        type: "tool_start",
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        label: label.active,
        done_label: label.done,
      });
    }
    if (event.type === "tool_execution_end") {
      const label = toolStatusLabel(event.toolName);
      const details = event.result?.details;
      const toolFailed = Boolean(event.isError || details?.ok === false);
      logManagerEvent("tool_execution_end", {
        run_id: runId,
        project_id: projectId,
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        is_error: toolFailed,
        details_bytes: payloadSize(details),
        ...summarizeToolPayload(event.toolName, details),
      });
      emit({
        type: "tool_end",
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        label: toolFailed ? `${label.active}失败` : label.done,
        done_label: label.done,
        is_error: toolFailed,
      });
      const report = buildToolReport(event.toolName, details);
      if (report) {
        emit({
          type: "tool_report",
          tool_name: event.toolName,
          tool_call_id: event.toolCallId,
          summary: report.summary,
          details: report.details,
        });
      }
    }
    if (event.type === "message_end" && event.message?.role === "assistant") {
      finalAssistantMessage = event.message;
      const tokenUsage = normalizeTokenUsage(event.message.usage, model);
      if (tokenUsage) {
        finalTokenUsage = tokenUsage;
        emit({
          type: "usage",
          usage: tokenUsage,
        });
      }
    }
  });
  const userEnvelope = {
    user_request: payload.message,
    session_id: payload.session_id || null,
    auto_mode: payload.auto_mode || { enabled: false, btw_mode: false },
    selected_context: payload.context || {},
    script_preference_guidance: scriptPreferenceGuidance(payload.context?.script_preference),
    runtime_preference_guidance: runtimePreferenceGuidance(payload.context || {}),
    instruction:
      (payload.auto_mode?.btw_mode
        ? "This session is in /btw mode. Answer questions, inspect status, explain logs, and use read-only tools only. Do not mutate blueprint or execution state."
        : "Answer naturally. Decide whether project tools are needed. If the user asks to continue, resume, run the next step, run ready cards, or start multiple cards, read get_background_workboard first and prefer the workboard claim/submit path. If you change the blueprint, remember that cards are the blueprint units and use create_card, revise_card_plan, annotate_card, delete_card, configure_card_execution, run-control, or template tools directly as needed. After ok:false tool results, correct and retry when the fix is clear.") +
      (payload.auto_mode?.enabled && !payload.auto_mode?.btw_mode
        ? " Auto episode is active for this session. The backend will send workboard_evaluate wakes when the workboard has fuel (todo items, completion signals, or blocker signals) and complete_evaluate wakes when no direct workboard fuel remains. On each wake, call get_background_workboard first, consume at most one item or batch per turn, and stop after reporting ids to let async-boundary yield the turn. When you receive a complete_evaluate wake, treat it as a closure/frontier decision, not proof of completion: inspect the project state and uploaded/materialized assets; if this is a new uploaded-data exploration objective and no suitable card exists, create and start a small exploration card; if a completed exploration output has no downstream card but the objective still needs work, create the next planned card from that output. Only call finish_auto_episode when the objective is truly done and no follow-up card should be created or started. Todo items only accept planned cards; the only explicit todo exit is skip (not done); todo items auto-exit when their card is submitted or started. If a tool returns pending_approvals, rejected_approvals, manual_preparation_required, partial_resolution, fallback_available, unsupported_source_spec, or runtime_missing, do not retry or ask the user — skip/defer/block the workboard item with the reason and move on."
        : ""),
  };
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => {
    logManagerEvent("run_timeout", {
      run_id: runId,
      project_id: projectId,
      elapsed_ms: Date.now() - runStartedAt,
      last_agent_event_type: lastAgentEventType,
      idle_ms: Date.now() - lastAgentEventAt,
    });
    abortController.abort(new Error("manager_timeout"));
  }, TIMEOUT_MS);
  const heartbeatId =
    emitEvent &&
    setInterval(() => {
      if (Date.now() - lastEmitAt < HEARTBEAT_INTERVAL_MS) {
        return;
      }
      emit({
        type: "heartbeat",
        stage: "waiting_for_model",
        message: "Manager 正在生成回复…",
      });
    }, HEARTBEAT_INTERVAL_MS);
  const waitLogId = setInterval(() => {
    logManagerEvent("run_waiting", {
      run_id: runId,
      project_id: projectId,
      elapsed_ms: Date.now() - runStartedAt,
      last_agent_event_type: lastAgentEventType,
      idle_ms: Date.now() - lastAgentEventAt,
      streamed_chars: streamedText.length,
      event_count: events.length,
    });
  }, WAIT_LOG_INTERVAL_MS);
  const abort = abortController.signal;
  const run = agent.prompt(JSON.stringify(userEnvelope, null, 2));
  if (externalAbortSignal) {
    if (externalAbortSignal.aborted) {
      abortController.abort(externalAbortSignal.reason);
    } else {
      externalAbortSignal.addEventListener("abort", () => abortController.abort(externalAbortSignal.reason), { once: true });
    }
  }
  abort.addEventListener("abort", () => agent.abort(), { once: true });
  try {
    await run;
  } catch (runError) {
    clearTimeout(timeoutId);
    clearInterval(waitLogId);
    if (heartbeatId) {
      clearInterval(heartbeatId);
    }
    await settleAutoTurnBestEffort();
    throw runError;
  } finally {
    clearTimeout(timeoutId);
    clearInterval(waitLogId);
    if (heartbeatId) {
      clearInterval(heartbeatId);
    }
  }
  for (const [blockKey, content] of thinkingBlocks.entries()) {
    if (completedThinkingBlocks.has(blockKey)) {
      continue;
    }
    const [turnIndexRaw, contentIndexRaw] = blockKey.split(":");
    emit({
      type: "thinking_end",
      content: content || "",
      content_index: Number(contentIndexRaw),
      assistant_turn_index: Number(turnIndexRaw),
      started_at: thinkingStartedAt.get(blockKey),
      ended_at: Date.now(),
    });
    completedThinkingBlocks.add(blockKey);
  }
  const thinking = collectThinking(thinkingBlocks) || undefined;
  const turnControl = toolControl.getTurnControl();
  const streamedOrFinalText = streamedText.trim() || extractText(finalAssistantMessage).trim();
  const syntheticAsyncBoundaryText = streamedOrFinalText ? "" : buildAsyncBoundaryTurnMessage(turnControl);
  const text = streamedOrFinalText || syntheticAsyncBoundaryText;
  const stopReason = finalAssistantMessage?.stopReason;
  if (stopReason === "error" || stopReason === "aborted") {
    logManagerEvent("run_error", {
      run_id: runId,
      project_id: projectId,
      elapsed_ms: Date.now() - runStartedAt,
      stop_reason: stopReason,
      error: finalAssistantMessage.errorMessage || `Manager agent stopped with ${stopReason}`,
      event_count: events.length,
    });
    await settleAutoTurnBestEffort();
    throw new Error(finalAssistantMessage.errorMessage || `Manager agent stopped with ${stopReason}`);
  }
  if (!text) {
    logManagerEvent("run_error", {
      run_id: runId,
      project_id: projectId,
      elapsed_ms: Date.now() - runStartedAt,
      error: "Manager agent returned an empty response.",
      event_count: events.length,
    });
    await settleAutoTurnBestEffort();
    throw new Error("Manager agent returned an empty response.");
  }
  await settleAutoTurnBestEffort(Boolean(turnControl.async_boundary?.active));
  logManagerEvent("run_done", {
    run_id: runId,
    project_id: projectId,
    elapsed_ms: Date.now() - runStartedAt,
    outcome: syntheticAsyncBoundaryText ? "async_boundary" : "text",
    event_count: events.length,
    streamed_chars: streamedText.length,
    total_tokens: finalTokenUsage?.total_tokens,
  });
  return {
    message: text,
    thinking,
    proposal: null,
    actions: [],
    warnings: [],
    metadata: {
      ...(finalTokenUsage ? { token_usage: finalTokenUsage } : {}),
      ...(turnControl?.async_boundary?.active ? { async_boundary: true, async_boundary_tool: turnControl.async_boundary.toolName || null, async_boundary_job_id: turnControl.async_boundary.jobId || null } : {}),
    },
  };
}

async function runManualCompaction(payload) {
  const runtimeConfig = resolveManagerConfig(payload);
  if (!runtimeConfig.apiKey) {
    throw new Error("MANAGER_AGENT_API_KEY or BLUEPRINT_DEEPSEEK_API_KEY is not configured.");
  }
  const model = resolveModel(runtimeConfig);
  if (!model) {
    throw new Error(`Manager model not found: provider=${runtimeConfig.provider}, model=${runtimeConfig.model}`);
  }
  const result = await maybeCompactSessionHistory({
    sessionMessages: payload.session_messages || [],
    model,
    thinkingEffort: payload.thinking_effort || "medium",
    signal: undefined,
    auto: false,
    force: true,
    runtimeConfig,
  });
  if (!result.compacted) {
    throw new Error("当前上下文还不需要压缩。");
  }
  return {
    compact_id: result.compactId,
    summary: result.summary,
    first_kept_message_id: result.firstKeptMessageId,
    tokens_before: result.tokensBefore,
    tokens_after: result.tokensAfter,
    duration_ms: result.durationMs,
    provider: result.provider,
    model: result.model,
  };
}

async function handle(req, res) {
  try {
    const { pathname } = new URL(req.url || "/", `http://${req.headers.host || "127.0.0.1"}`);
    if (req.method === "GET" && pathname === "/healthz") {
      const validation = validateStartupConfig();
      if (!validation.ok) {
        jsonResponse(res, 503, { status: "not_ready", ready: false, errors: validation.errors });
        return;
      }
      jsonResponse(res, 200, { status: "ok", ready: true });
      return;
    }
    if (req.method === "POST" && pathname === "/chat-stream") {
      const disconnectController = new AbortController();
      req.on("close", () => disconnectController.abort(new Error("client_disconnected")));
      openSse(res);
      try {
        const payload = await readJson(req);
        const response = await runManagerChat(payload, (event) => writeSseEvent(res, event), disconnectController.signal);
        writeSseEvent(res, { type: "response", response });
        writeSseEvent(res, { type: "done" });
      } catch (error) {
        writeSseEvent(res, { type: "error", detail: error instanceof Error ? error.message : String(error) });
      } finally {
        sseStreams.delete(res);
        if (!res.destroyed && !res.writableEnded) {
          res.end();
        }
      }
      return;
    }
    if (req.method === "POST" && pathname === "/compact") {
      try {
        const payload = await readJson(req);
        const response = await runManualCompaction(payload);
        jsonResponse(res, 200, response);
      } catch (error) {
        jsonResponse(res, 502, { detail: error instanceof Error ? error.message : String(error) });
      }
      return;
    }
    if (req.method !== "POST" || pathname !== "/chat") {
      jsonResponse(res, 404, { detail: "Not found" });
      return;
    }
    try {
      const payload = await readJson(req);
      const response = await runManagerChat(payload);
      jsonResponseWithHeaders(res, 200, response, {
        "Deprecation": "true",
        "Link": "</chat-stream>; rel=\"successor-version\"",
      });
    } catch (error) {
      jsonResponse(res, 502, { detail: error instanceof Error ? error.message : String(error) });
    }
  } catch (error) {
    if (!res.headersSent) {
      jsonResponse(res, 500, { detail: error instanceof Error ? error.message : String(error) });
    } else if (!res.writableEnded && !res.destroyed) {
      res.end();
    }
  }
}

const validation = validateStartupConfig();
if (!validation.ok) {
  console.error("Manager agent startup validation failed:");
  for (const err of validation.errors) {
    console.error("  - " + err);
  }
  process.exit(1);
}

const server = (await import("node:http")).createServer((req, res) => {
  handle(req, res).catch((error) => {
    console.error("Unhandled error in request handler:", error);
    if (!res.headersSent) {
      jsonResponse(res, 500, { detail: "Internal server error" });
    } else if (!res.writableEnded && !res.destroyed) {
      res.end();
    }
  });
});

server.listen(PORT, HOST, () => {
  console.log(`Blueprint Manager Pi agent listening on http://${HOST}:${PORT}`);
});
