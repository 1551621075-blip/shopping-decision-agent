/* Local-only UI. Dynamic values are always text nodes, never HTML. */
(() => {
  "use strict";

  function yuanToCents(value) {
    if (typeof value !== "string" || !/^(0|[1-9]\d*)(?:\.\d{1,2})?$/.test(value)) {
      throw new Error("金额须为非负十进制元，最多两位小数；不接受指数、空格或自动舍入。");
    }
    const [whole, fraction = ""] = value.split(".");
    const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
    if (cents > 10000000n) throw new Error("金额超过演示上限100000元。");
    return Number(cents);
  }

  function integerInput(value, min, max, nullable = false) {
    if (value === "" && nullable) return null;
    if (!/^(0|[1-9]\d*)$/.test(value)) throw new Error("数量须为明确的十进制整数；未知请勿填0。");
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < min || number > max) {
      throw new Error(`整数范围应为 ${min} 至 ${max}。`);
    }
    return number;
  }

  function orderRecords(records, messages) {
    const references = new Map();
    let sequence = 0;
    for (const message of messages || []) {
      for (const id of [message.proposal_id, message.recommendation_id, ...(message.evidence_ids || [])]) {
        if (id && !references.has(id)) references.set(id, sequence++);
      }
    }
    return records.map((record, index) => ({record, index})).sort((a, b) => {
      const stamp = item => item.payload.captured_at ?? item.payload.started_at ??
        (item.payload.expires_at ? item.payload.expires_at - 900 : 0);
      const difference = stamp(a.record) - stamp(b.record);
      if (difference) return difference;
      if (references.has(a.record.id) && references.has(b.record.id)) {
        return references.get(a.record.id) - references.get(b.record.id);
      }
      // Keep stable server sequence when there is no chronological evidence, never sort random IDs.
      return a.index - b.index;
    }).map(item => item.record);
  }

  function createOperation(path, payload, actionId) {
    return {path, body: JSON.parse(JSON.stringify({...payload, action_id: actionId}))};
  }

  function placeAt(parent, node, index) {
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {yuanToCents, integerInput, orderRecords, createOperation, placeAt};
  }
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const state = {
    token: "", me: null, sid: "", view: null, sessions: [], catalog: [], unknown: [],
    busy: false, pending: null, polling: false, epoch: 0, pointer: false, deferred: false,
    prefsDirty: false, prefsRevision: null, prefsBase: null, verified: false,
  };
  const prefFields = {
    min_ram_gb: "min-ram-gb", min_storage_gb: "min-storage-gb", max_weight_g: "max-weight-g",
    min_battery_minutes: "min-battery-minutes", use: "use", priority: "priority", os: "os",
  };
  const names = {office: "办公", coding: "编程", creative: "创作", gaming: "游戏", value: "价格 / 性价比",
    performance: "性能", portability: "便携", battery: "续航", any: "不限", windows: "Windows", linux: "Linux",
    macos: "macOS", confirmed: "已确认", declined: "已拒绝", superseded: "已被后续操作替代",
    pending_confirmation: "等待精确确认", completed: "已完成", waiting_customer: "等待客户消息",
    waiting_confirmation: "等待偏好确认", blocked_unknown: "未知调用阻断，需operator对账",
    cancelled: "已取消", running: "运行中", queued: "排队中", provider_unavailable: "模型不可用",
    interrupted: "进程中断", reconciled: "已对账", budget_exhausted: "轮次预算耗尽",
    stale_evidence: "条件、目录或来源已变化 / 证据过期", expired: "已过期", better: "更优", worse: "更弱", equal: "相同"};
  const label = value => names[value] || value || "未知";
  const money = cents => Number.isSafeInteger(cents) ? `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}` : "未知";
  const text = (node, value) => { node.textContent = String(value ?? "未知"); return node; };
  const el = (tag, value, className) => {
    const node = document.createElement(tag);
    if (value !== undefined) text(node, value);
    if (className) node.className = className;
    return node;
  };
  function notice(message, error = false) {
    text($("status"), message);
    $("status").className = error ? "error" : "notice";
  }
  function controls() {
    for (const node of document.querySelectorAll("button, input, select, textarea")) {
      node.disabled = state.busy || Boolean(state.pending);
    }
    $("logout").disabled = state.busy;
    $("retry-action").disabled = state.busy;
    $("retry-panel").hidden = !state.pending;
    if (!state.busy && !state.pending) {
      $("cancel-run").disabled = !state.view?.session.payload.active_run;
      $("save-preferences").disabled = !state.sid || !state.verified;
      $("reload-preferences").disabled = !state.sid;
      $("send-message").disabled = !state.sid || Boolean(state.view?.session.payload.active_run) ||
        Boolean(state.view?.session.payload.blocked_unknown);
      for (const button of document.querySelectorAll('[data-action="confirm"], [data-action="decline"]')) {
        button.disabled = !state.verified || button.closest("[data-valid]").dataset.valid !== "true";
      }
      for (const button of document.querySelectorAll('[data-action="update-product"]')) {
        button.disabled = !state.verified;
      }
      for (const button of document.querySelectorAll('[data-action="reconcile"]')) {
        button.disabled = button.closest("[data-unknown]").dataset.active !== "true";
      }
    }
  }

  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        method: body === undefined ? "GET" : "POST", mode: "same-origin", credentials: "omit",
        cache: "no-store", redirect: "error", signal: controller.signal,
        headers: {Authorization: `Bearer ${state.token}`, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
        ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      });
      let result;
      try { result = await response.json(); } catch (_) { throw new Error("响应格式异常，操作结果尚不明确。"); }
      if (!response.ok) {
        const error = new Error(result.error?.message || "请求失败，请核对当前状态。");
        error.status = response.status;
        throw error;
      }
      return result;
    } finally { clearTimeout(timeout); }
  }

  function applySession(session) {
    if (session && session.id === state.sid && state.view) {
      state.view.session = session; // The server version is authoritative; never increment it here.
    }
  }
  async function executePending() {
    if (state.busy || !state.pending) return;
    const pending = state.pending;
    state.busy = true;
    state.epoch++;
    controls();
    try {
      const result = await request(pending.operation.path, pending.operation.body);
      state.pending = null;
      if (result.kind === "session") applySession(result);
      if (result.session) applySession(result.session);
      pending.success(result);
      notice(result.notice || result.message || "操作已保存。仅发送新消息才会启动模型。");
    } catch (error) {
      if (error.status && error.status < 500 && error.status !== 408) {
        state.pending = null;
        // A definite rejection can be submitted afresh only after the user reviews it.
        notice(`${error.message} 输入已保留；请核对刷新后的状态再显式提交。`, true);
      } else {
        notice("网络或服务响应异常，结果尚不明确。请使用原action_id重试；未自动重发。", true);
      }
    } finally {
      state.busy = false;
      controls();
      await refresh();
    }
  }
  function mutate(path, payload, success = () => {}) {
    if (state.busy || state.pending) return;
    state.pending = {operation: createOperation(path, payload, `action-${crypto.randomUUID()}`), success};
    void executePending();
  }
  function guardInput(fn) {
    try { fn(); } catch (error) { notice(error.message, true); }
  }

  function preferencesSummary(prefs) {
    if (!prefs) return "尚未确认偏好";
    return `总预算 ¥${money(prefs.budget_cents)}（含运费）｜${label(prefs.use)}｜优先${label(prefs.priority)}\n` +
      `内存 ≥${prefs.min_ram_gb}GB；存储 ≥${prefs.min_storage_gb}GB；` +
      `重量 ${prefs.max_weight_g === null ? "不限" : `≤${prefs.max_weight_g}克`}；` +
      `合成续航 ${prefs.min_battery_minutes === null ? "不限" : `≥${prefs.min_battery_minutes}分钟`}；` +
      `独显 ${prefs.needs_discrete_gpu ? "必须" : "不限"}；系统 ${label(prefs.os)}`;
  }
  function readPreferences() {
    const budget = yuanToCents($("budget-yuan").value);
    if (budget < 10000) throw new Error("总预算不得低于100元。");
    return {budget_cents: budget, use: $("use").value, priority: $("priority").value,
      min_ram_gb: integerInput($("min-ram-gb").value, 4, 128),
      min_storage_gb: integerInput($("min-storage-gb").value, 128, 8192),
      max_weight_g: integerInput($("max-weight-g").value, 500, 5000, true),
      min_battery_minutes: integerInput($("min-battery-minutes").value, 30, 1440, true),
      needs_discrete_gpu: $("needs-discrete-gpu").checked, os: $("os").value};
  }
  function loadPreferences(session) {
    const prefs = session.payload.preferences;
    if (prefs) {
      $("budget-yuan").value = money(prefs.budget_cents);
      for (const [field, id] of Object.entries(prefFields)) $(id).value = prefs[field] ?? "";
      $("needs-discrete-gpu").checked = prefs.needs_discrete_gpu;
    } else {
      $("preferences-form").reset();
    }
    state.prefsRevision = session.payload.preferences_revision;
    state.prefsBase = {version: session.version, revision: session.payload.preferences_revision};
  }
  function renderPreferences() {
    const session = state.view.session;
    const prefs = session.payload.preferences;
    const conflict = state.prefsDirty && state.prefsBase?.revision !== session.payload.preferences_revision;
    text($("preferences-status"), `${preferencesSummary(prefs)}\n` + (conflict
      ? "服务端偏好已被其他操作修改；你的输入已保留。请先明确重新加载最新偏好，再编辑提交。"
      : state.prefsDirty ? `有未提交修改，绑定编辑起点版本 ${state.prefsBase?.version}；轮询不会覆盖或自动重基。`
        : `服务端偏好修订 ${session.payload.preferences_revision}`));
    // Do not touch values on blur or advance the edit fence behind a focused/dirty form.
    if (state.prefsDirty || $("preferences-form").contains(document.activeElement)) return;
    if (!state.prefsBase || state.prefsRevision !== session.payload.preferences_revision) loadPreferences(session);
    state.prefsBase = {version: session.version, revision: session.payload.preferences_revision};
  }

  async function explicitReload(load) {
    if (state.busy || state.pending || state.polling) {
      notice("已有操作或刷新进行中，输入已保留；请稍后再次明确重新加载。", true);
      return;
    }
    state.busy = true;
    const epoch = ++state.epoch;
    controls();
    try {
      const apply = await load();
      if (epoch === state.epoch) {
        apply();
        notice("已明确重新加载最新值并放弃该表单的旧输入；尚未提交任何修改。请核对后再编辑提交。");
      }
    } catch (_) {
      state.verified = false;
      notice("重新加载失败，旧输入仍保留；未核验最新状态，未提交修改。", true);
    } finally {
      state.busy = false;
      render();
    }
  }

  function productDetails(row) {
    const p = row.product || row;
    const node = el("div", undefined, "product");
    node.dataset.productId = p.id;
    node.append(el("h4", `${p.name} · ${p.id}`));
    node.append(el("p", `商品 ¥${money(p.price_cents)} + 运费 ¥${money(p.shipping_cents)} = ` +
      `总成本 ¥${money(row.total_cents ?? (p.price_cents + p.shipping_cents))}；库存 ${p.stock}`));
    node.append(el("p", `内存 ${p.ram_gb ?? "未知"}GB · 存储 ${p.storage_gb ?? "未知"}GB · ` +
      `重量 ${p.weight_g ?? "未知"}克 · 合成续航 ${p.battery_minutes ?? "未知"}分钟 · ` +
      `CPU ${p.cpu_score ?? "未知"} · GPU ${p.gpu_score ?? "未知"} · ` +
      `独显 ${p.discrete_gpu === null ? "未知" : p.discrete_gpu ? "有" : "无"} · 系统 ${label(p.os)}`));
    if (row.score !== undefined) node.append(el("p", `确定性得分 ${row.score}，仅限本次比较候选。`));
    if (row.components) node.append(el("pre", JSON.stringify(row.components, null, 2)));
    return node;
  }
  const recordNodes = new Map();
  function renderRecords(containerId, records) {
    const container = $(containerId);
    let position = 0;
    for (const record of orderRecords(records, state.view.session.payload.messages)) {
      const key = `${record.kind}:${record.id}`;
      let card = recordNodes.get(key);
      if (!card) {
        card = el("article", undefined, "card");
        card.dataset.id = record.id;
        card.dataset.kind = record.kind;
        const title = el("h3"); title.dataset.part = "title";
        const validity = el("p", undefined, "validity"); validity.dataset.part = "validity";
        const content = el("div"); content.dataset.part = "content";
        card.append(title, validity, content);
        if (record.kind === "proposal") {
          for (const decision of ["confirm", "decline"]) {
            const button = el("button", decision === "confirm" ? "精确确认此草案（不启动模型）" : "拒绝此草案");
            button.type = "button";
            button.dataset.action = decision;
            button.addEventListener("click", () => {
              const snapshot = card.record;
              if (!state.verified || card.dataset.valid !== "true" || !snapshot.display_valid) return;
              mutate(`/api/proposals/${snapshot.id}/decision`, {expected_version: snapshot.version,
                content_hash: snapshot.content_hash, decision});
            });
            card.append(button);
          }
        }
        recordNodes.set(key, card);
        container.append(card);
      }
      card.record = record;
      card.dataset.verified = String(state.verified);
      card.dataset.valid = String(state.verified && record.display_valid === true);
      card.dataset.version = String(record.version);
      card.dataset.hash = record.content_hash;
      card.dataset.tool = record.payload.tool || "";
      const p = record.payload;
      const heading = {proposal: "偏好草案", evaluation: "后端评估", recommendation: "推荐与取舍"}[record.kind] ||
        {search_products: "查询", compare_products: "比较", read_context: "上下文快照"}[p.tool] || "证据";
      text(card.querySelector('[data-part="title"]'), `${heading} · ${record.id} · v${record.version}`);
      text(card.querySelector('[data-part="validity"]'), !state.verified
        ? "未核验 / 只读刷新失败；保留历史内容，不作为当前有效建议，禁止确认。"
        : record.display_valid
          ? `当前有效${p.state ? ` · ${label(p.state)}` : ""}${p.confirmed === false ? " · 临时条件，尚未经用户确认" : ""}`
          : `历史记录 / 无效，不可作为当前建议 · ${label(record.display_reason || p.state || "expired")}`);
      const signature = JSON.stringify(p);
      if (card.signature !== signature) {
        card.signature = signature;
        const content = card.querySelector('[data-part="content"]');
        content.replaceChildren();
        if (p.preferences) content.append(el("p", preferencesSummary(p.preferences)));
        if (record.kind === "recommendation") {
          if (p.selection) content.append(productDetails(p.selection));
          content.append(el("h4", "后端核验事实"));
          for (const fact of p.facts || []) content.append(el("p", `${fact.label}：${fact.value}`));
          content.append(el("h4", "相对其他候选的实际取舍"));
          for (const trade of p.tradeoffs || []) content.append(el("p", `${trade.against_product_id} · ${trade.label}：` +
            `所选 ${trade.selected_value} / 对方 ${trade.other_value}，${label(trade.relation)}`));
          content.append(el("p", `评分协议 ${p.scoring_version}；最高分候选 ${p.top_product_id}。${p.notice || ""}`));
        } else if (p.data) {
          const products = p.data.eligible || p.data.products || [];
          for (const row of products) content.append(productDetails(row));
          if (p.tool === "search_products" && products.length === 0) content.append(el("p", "无合格商品；预算不会自动增加。", "warning"));
          if (p.data.excluded?.length) {
            content.append(el("h4", "排除原因"));
            for (const excluded of p.data.excluded) content.append(el("pre", JSON.stringify(excluded, null, 2)));
          }
          const details = el("details");
          details.append(el("summary", "核对完整后端结构与评分说明"), el("pre", JSON.stringify(p.data, null, 2)));
          content.append(details);
        }
      }
      // Unchanged cards never move during polling, preserving keyboard focus as well as clicks.
      placeAt(container, card, position++);
    }
  }

  function renderSessionList() {
    const select = $("session-list");
    for (const session of state.sessions) {
      let option = Array.from(select.options).find(item => item.value === session.id);
      if (!option) { option = el("option"); option.value = session.id; select.append(option); }
      text(option, session.id);
    }
    select.value = state.sid;
  }
  function renderCustomer() {
    renderSessionList();
    if (!state.view) return;
    const session = state.view.session;
    const runs = state.view.runs || [];
    const active = runs.find(run => run.id === session.payload.active_run);
    text($("session-status"), `${session.id} · 服务端版本 ${session.version} · ` +
      (session.payload.blocked_unknown ? label("blocked_unknown") : active ? label(active.payload.state) : "无活动轮次，可发送新消息"));
    text($("model-calls"), runs.reduce((sum, run) => sum + run.payload.calls, 0));
    const messages = $("messages");
    session.payload.messages.forEach((message, index) => {
      let node = messages.children[index];
      if (!node) { node = el("p", undefined, "message"); messages.append(node); }
      node.dataset.messageKind = message.kind || message.role;
      node.dataset.role = message.role;
      text(node, `${message.role === "customer" ? "你" : "助手"}：${message.text}`);
    });
    renderPreferences();
    for (const collection of ["proposals", "evidences", "evaluations", "recommendations"]) {
      renderRecords(collection, state.view[collection] || []);
    }
  }

  function field(form, id, title, value, type = "text") {
    const caption = el("label", title); caption.htmlFor = id;
    const input = el("input"); input.id = id; input.type = type; input.value = value;
    input.required = true;
    form.append(caption, input);
    return input;
  }
  const catalogNodes = new Map();
  const unknownNodes = new Map();
  function renderOperator() {
    for (const record of state.catalog) {
      let form = catalogNodes.get(record.id);
      if (!form) {
        form = el("form", undefined, "card"); form.dataset.productId = record.id;
        form.append(el("h3", record.payload.name));
        form.append(el("p", "仅合成价格与库存；总成本另计明示运费。"));
        field(form, `price-${record.id}`, "商品价格（元）", money(record.payload.price_cents));
        field(form, `stock-${record.id}`, "库存（整数）", record.payload.stock, "number");
        const button = el("button", "保存合成价格与库存"); button.type = "submit"; button.dataset.action = "update-product";
        form.append(button);
        const reload = el("button", "放弃本商品未提交输入，重新加载最新价格与库存");
        reload.type = "button"; reload.dataset.action = "reload-product";
        reload.addEventListener("click", () => { void explicitReload(async () => {
          const latest = await request("/api/catalog");
          const current = latest.find(item => item.id === record.id);
          if (!current) throw new Error("商品已不存在");
          return () => {
            state.catalog = latest;
            form.dirty = false; form.record = current;
            $(`price-${record.id}`).value = money(current.payload.price_cents);
            $(`stock-${record.id}`).value = current.payload.stock;
          };
        }); });
        const conflict = el("p"); conflict.dataset.part = "conflict";
        form.append(reload, conflict);
        form.addEventListener("input", () => { form.dirty = true; });
        form.addEventListener("submit", event => {
          event.preventDefault();
          if (!state.verified) return;
          guardInput(() => mutate(`/api/catalog/${record.id}`, {
            expected_version: form.record.version, price_cents: yuanToCents($(`price-${record.id}`).value),
            stock: integerInput($(`stock-${record.id}`).value, 0, 10000),
          }, result => { form.dirty = false; form.record = result; }));
        });
        catalogNodes.set(record.id, form);
        $("catalog").append(form);
      }
      if (!form.dirty && !form.contains(document.activeElement)) {
        form.record = record;
        $(`price-${record.id}`).value = money(record.payload.price_cents);
        $(`stock-${record.id}`).value = record.payload.stock;
      } else if (!form.record) form.record = record;
      form.dataset.version = String(record.version);
      text(form.querySelector('[data-part="conflict"]'), form.dirty && form.record.version !== record.version
        ? `服务端已是v${record.version}，输入仍绑定v${form.record.version}。请明确重新加载后再编辑，禁止自动覆盖。`
        : `编辑基准v${form.record.version}；重新加载会明确放弃此商品的未提交输入。`);
    }
    for (const form of unknownNodes.values()) form.dataset.active = "false";
    for (const call of state.unknown) {
      let form = unknownNodes.get(call.id);
      if (!form) {
        form = el("form", undefined, "card warning"); form.dataset.unknown = call.id;
        form.append(el("h3", `未知调用 ${call.id} · ${call.usage_kind === "synthetic" ? "TEST合成用量" : "真实用量待核对"}`));
        field(form, `tokens-${call.id}`, "依据调用记录核对的token数（未知不能填0代替）", call.tokens ?? "", "number");
        field(form, `basis-${call.id}`, "明确对账依据 / 记录位置及结论", "");
        const button = el("button", "确认无可用结果并对账（不重放）"); button.type = "submit"; button.dataset.action = "reconcile";
        form.append(button);
        form.addEventListener("submit", event => {
          event.preventDefault();
          guardInput(() => {
            const note = $(`basis-${call.id}`).value.trim();
            if (!note) throw new Error("对账必须填写明确依据。");
            mutate("/api/usage/reconcile", {call_id: call.id,
              tokens: integerInput($(`tokens-${call.id}`).value, 0, 10000000), note, outcome: "no_usable_result"});
          });
        });
        unknownNodes.set(call.id, form);
        $("unknown-calls").append(form);
      }
      form.dataset.active = "true";
    }
    for (const form of unknownNodes.values()) {
      let result = form.querySelector('[data-part="result"]');
      if (!result) { result = el("p"); result.dataset.part = "result"; form.append(result); }
      text(result, form.dataset.active === "true" ? "等待核对，不能自动补零。" : "此调用已不在未知列表；保留表单历史，禁止重复对账。");
    }
  }
  function render() {
    if (state.pointer || state.busy) { state.deferred = true; return; }
    state.deferred = false;
    if (!state.me) return;
    if (state.me.role === "customer") renderCustomer(); else renderOperator();
    controls();
  }
  async function refresh() {
    if (!state.me || state.busy || state.polling) return;
    state.polling = true;
    const epoch = state.epoch, sid = state.sid;
    try {
      if (state.me.role === "customer") {
        const [sessions, view] = await Promise.all([request("/api/sessions"), sid ? request(`/api/sessions/${sid}`) : null]);
        if (epoch !== state.epoch || sid !== state.sid) return;
        state.sessions = sessions;
        if (view) state.view = view;
      } else {
        const [catalog, unknown] = await Promise.all([request("/api/catalog"), request("/api/usage/unknown")]);
        if (epoch !== state.epoch) return;
        state.catalog = catalog; state.unknown = unknown;
      }
      state.verified = true;
      render();
    } catch (_) {
      if (epoch === state.epoch) {
        state.verified = false;
        notice("只读刷新失败，现有卡片未核验，已禁止确认；保留未提交输入，不会自动调用模型。", true);
        // Relabel existing nodes only; do not move a pending pointer/keyboard target.
        for (const card of recordNodes.values()) {
          card.dataset.verified = "false"; card.dataset.valid = "false";
          text(card.querySelector('[data-part="validity"]'), "未核验 / 只读刷新失败；历史内容不作为当前有效建议，禁止确认。");
        }
        controls();
      }
    } finally { state.polling = false; }
  }

  function clearSession() {
    state.view = null; state.prefsDirty = false; state.prefsRevision = null;
    state.prefsBase = null; state.verified = false;
    recordNodes.clear();
    for (const id of ["messages", "proposals", "evidences", "evaluations", "recommendations"]) $(id).replaceChildren();
    $("preferences-form").reset(); $("message-form").reset();
    text($("preferences-status"), "尚未确认；表单初值不会自动生效。");
    text($("model-calls"), "0");
  }
  function leaveDirty() {
    if (state.prefsDirty || $("message-text").value.trim()) {
      notice("请先提交当前偏好或消息；未提交输入不会因切换会话丢失。", true);
      return true;
    }
    return false;
  }
  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (state.busy) return;
    state.token = $("token").value; $("token").value = "";
    state.busy = true; state.epoch++; controls();
    try {
      state.me = await request("/api/me");
      text($("identity"), `${state.me.user_id} · ${state.me.role === "customer" ? "客户" : "目录与对账操作员"}`);
      text($("mode-banner"), state.me.test_mode ? "TEST · 显式脚本与合成用量，不是真实模型验收" : "本地真实模型模式 · 尚不代表验收通过");
      $("login-panel").hidden = true; $("identity-panel").hidden = false;
      $("customer-panel").hidden = state.me.role !== "customer";
      $("operator-panel").hidden = state.me.role !== "operator";
      document.body.dataset.ready = "true";
      notice("登录成功。所有刷新只读；令牌仅保存在此页面内存。");
    } catch (_) {
      state.token = ""; state.me = null;
      notice("登录失败，请核对本实例身份令牌。", true);
    } finally { state.busy = false; controls(); await refresh(); }
  });
  $("logout").addEventListener("click", () => {
    if (state.busy) return;
    state.token = ""; state.me = null; state.pending = null; state.sid = ""; state.epoch++;
    clearSession(); catalogNodes.clear(); unknownNodes.clear();
    $("catalog").replaceChildren(); $("unknown-calls").replaceChildren();
    state.sessions = []; state.catalog = []; state.unknown = [];
    $("session-list").replaceChildren();
    const empty = el("option", "请选择会话"); empty.value = ""; $("session-list").append(empty);
    for (const id of ["identity-panel", "customer-panel", "operator-panel", "retry-panel"]) $(id).hidden = true;
    $("login-panel").hidden = false; document.body.dataset.ready = "false";
    text($("identity"), ""); text($("mode-banner"), "已退出 · TEST 脚本不代表真实模型验收");
    notice("已清除页面身份和会话显示。"); controls();
  });
  $("new-session").addEventListener("click", () => {
    if (leaveDirty()) return;
    mutate("/api/sessions", {}, result => { clearSession(); state.sid = result.id; });
  });
  $("session-list").addEventListener("change", () => {
    if (state.busy || state.pending || leaveDirty()) { $("session-list").value = state.sid; return; }
    state.sid = $("session-list").value; state.epoch++; clearSession(); void refresh();
  });
  $("message-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!state.view || !$("message-text").value.trim()) return;
    mutate(`/api/sessions/${state.sid}/messages`, {expected_version: state.view.session.version,
      text: $("message-text").value}, () => { $("message-text").value = ""; });
  });
  $("cancel-run").addEventListener("click", () => {
    if (state.view) mutate(`/api/sessions/${state.sid}/cancel`, {expected_version: state.view.session.version});
  });
  const markPreferencesDirty = () => { state.prefsDirty = true; };
  $("preferences-form").addEventListener("input", markPreferencesDirty);
  $("preferences-form").addEventListener("change", markPreferencesDirty);
  $("preferences-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!state.view || !state.verified || !state.prefsBase) return;
    guardInput(() => {
      if (state.prefsBase.revision !== state.view.session.payload.preferences_revision) {
        throw new Error("偏好已被其他操作修改。输入已保留，请明确重新加载最新偏好后再编辑提交。");
      }
      mutate(`/api/sessions/${state.sid}/preferences`, {expected_version: state.prefsBase.version,
        preferences: readPreferences()}, result => {
        state.prefsDirty = false;
        loadPreferences(result);
      });
    });
  });
  $("reload-preferences").addEventListener("click", () => {
    if (!state.sid) return;
    void explicitReload(async () => {
      const view = await request(`/api/sessions/${state.sid}`);
      return () => {
        state.view = view; state.verified = true; state.prefsDirty = false;
        loadPreferences(view.session);
      };
    });
  });
  $("refresh").addEventListener("click", () => { void refresh(); });
  $("retry-action").addEventListener("click", () => { void executePending(); });
  // Never rerender on blur/focusout: pointerup's deferred task runs after the pending click.
  document.addEventListener("pointerdown", () => { state.pointer = true; }, true);
  const release = () => setTimeout(() => { state.pointer = false; if (state.deferred) render(); }, 0);
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);
  window.addEventListener("blur", release);
  setInterval(() => { if (!document.hidden) void refresh(); }, 1000);
  controls();
})();
