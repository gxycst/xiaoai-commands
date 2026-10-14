"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { authenticated: false, csrfToken: "", rules: null, version: null, dirty: false, conflict: false, readError: false, saving: false, loading: false, testing: false, loginBusy: false, editingIndex: null, revision: 0 };
  const nodes = {};
  ["login-panel", "app-panel", "initial-status", "logout-button", "password", "login-button", "login-error", "login-description", "rules-list", "rule-count", "search-input", "search-summary", "add-button", "save-button", "reload-button", "draft-status", "backup-status", "app-notice", "conflict-panel", "test-button", "test-source", "test-result", "test-query", "edit-dialog", "edit-title", "rule-name", "rule-keywords", "rule-reply", "rule-repeat", "edit-error"].forEach((id) => { nodes[id] = $(id); });

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function message(text, type = "info") {
    nodes["app-notice"].textContent = text;
    nodes["app-notice"].className = "notice" + (type === "error" ? " error" : type === "warning" ? " warning" : "");
    nodes["app-notice"].hidden = !text;
  }

  function showSession() {
    nodes["initial-status"].hidden = true;
    nodes["login-panel"].hidden = state.authenticated;
    nodes["app-panel"].hidden = !state.authenticated;
    nodes["logout-button"].hidden = !state.authenticated;
    nodes["login-description"].textContent = state.dirty ? "登录已过期，页面中未保存的修改还在。重新登录即可继续。" : "输入管理密码，打开你的小爱词库。";
    updateControls();
  }

  async function api(path, options = {}) {
    const headers = { "Accept": "application/json", ...options.headers };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.method && options.method !== "GET" && state.csrfToken) headers["X-CSRF-Token"] = state.csrfToken;
    const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, headers, body: options.body !== undefined ? JSON.stringify(options.body) : undefined });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务返回了无效内容，请检查服务是否正常。"); }
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/login") {
        state.authenticated = false;
        state.csrfToken = "";
        showSession();
        nodes.password.focus();
      }
      const error = new Error(response.status === 401 && path !== "/api/login" ? "登录已过期，重新登录后可以继续，未保存修改仍在页面中。" : (typeof data.error === "string" ? data.error : typeof data.message === "string" ? data.message : "操作失败，请稍后重试。"));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function updateControls() {
    const unavailable = !state.authenticated || state.rules === null || state.readError || state.loading || state.saving;
    nodes["add-button"].disabled = unavailable;
    nodes["save-button"].disabled = unavailable || !state.dirty || state.conflict;
    nodes["save-button"].textContent = state.saving ? "正在保存…" : "保存词库";
    nodes["reload-button"].disabled = state.loading || state.saving;
    nodes["logout-button"].disabled = state.saving || state.loading;
    nodes["test-button"].disabled = unavailable || state.testing;
    nodes["test-button"].textContent = state.testing ? "正在测试…" : "测试命中 →";
    nodes["draft-status"].className = "state-pill" + (state.conflict || state.readError || state.rules === null && !state.loading ? " error" : state.dirty ? " dirty" : "");
    nodes["draft-status"].textContent = state.loading ? "正在读取" : state.saving ? "正在保存" : state.readError || state.rules === null ? "读取失败" : state.conflict ? "保存冲突" : state.dirty ? "有未保存修改" : "已同步文件";
    nodes["test-source"].textContent = state.dirty ? "使用当前页面的未保存词库" : "使用已保存的词库";
    nodes["test-source"].className = "test-source" + (state.dirty ? " dirty" : "");
    nodes["conflict-panel"].hidden = !state.conflict;
    nodes["rule-count"].textContent = state.rules === null ? "— 条" : state.rules.length + " 条";
    nodes["rules-list"].querySelectorAll("button").forEach((button) => { button.disabled = unavailable || button.dataset.boundary === "true"; });
  }

  function markDirty() {
    state.dirty = true;
    state.revision += 1;
    nodes["test-result"].hidden = true;
    message("");
    renderRules();
    updateControls();
  }

  function makeButton(text, className, handler, label) {
    const button = element("button", className, text);
    button.type = "button";
    if (label) { button.setAttribute("aria-label", label); button.title = label; }
    button.addEventListener("click", handler);
    return button;
  }

  function renderRules() {
    const list = nodes["rules-list"];
    list.replaceChildren();
    const query = nodes["search-input"].value.toLocaleLowerCase();
    if (state.rules === null) {
      const empty = element("div", "empty-state");
      empty.append(element("strong", "", state.loading ? "正在读取词库…" : "词库暂时无法读取"), element("p", "", state.loading ? "请稍候。" : "检查 commands.json 后，点击「重新读取」。"));
      list.append(empty);
      nodes["search-summary"].hidden = true;
      return;
    }
    let visibleCount = 0;
    state.rules.forEach((rule, index) => {
      const searchable = [rule.name, rule.reply, ...rule.keywords].join("\n").toLocaleLowerCase();
      if (query && !searchable.includes(query)) return;
      visibleCount += 1;
      const card = element("article", "rule-card");
      const header = element("div", "rule-card-header");
      const title = element("div", "rule-name-wrap");
      title.append(element("span", "rule-order", String(index + 1).padStart(2, "0")), element("h4", "rule-name", rule.name));
      const order = element("div", "reorder-buttons");
      const up = makeButton("↑", "icon-button", () => moveRule(index, -1), "上移「" + rule.name + "」");
      const down = makeButton("↓", "icon-button", () => moveRule(index, 1), "下移「" + rule.name + "」");
      up.dataset.boundary = String(index === 0);
      down.dataset.boundary = String(index === state.rules.length - 1);
      order.append(up, down);
      header.append(title, order);
      const keywords = element("div", "keywords");
      keywords.setAttribute("aria-label", "触发关键词");
      rule.keywords.forEach((keyword) => keywords.append(element("span", "keyword", keyword)));
      const footer = element("div", "rule-card-footer");
      const actions = element("div", "card-actions");
      actions.append(makeButton("编辑", "text-button", () => editRule(index), "编辑「" + rule.name + "」"), makeButton("删除", "text-button danger", () => deleteRule(index), "删除「" + rule.name + "」"));
      footer.append(element("span", "repeat-tag", "重复 " + rule.repeat + " 次"), actions);
      card.append(header, keywords, element("p", "reply-label", "小爱的回复"), element("p", "reply", rule.reply), footer);
      list.append(card);
    });
    nodes["search-summary"].hidden = !query;
    nodes["search-summary"].textContent = "找到 " + visibleCount + " 条规则 · 编号保留词库中的原始顺序";
    if (visibleCount === 0) {
      const empty = element("div", "empty-state");
      empty.append(element("strong", "", query ? "没有找到相关规则" : "还没有互动规则"), element("p", "", query ? "换一个词搜索试试。" : "点击「添加规则」，写下第一句回应。"));
      list.append(empty);
    }
    updateControls();
  }

  async function loadRules(confirmDiscard = true) {
    if (state.loading || state.saving || !state.authenticated) return;
    if (confirmDiscard && state.dirty && !window.confirm("重新读取会放弃页面中未保存的修改。确定继续吗？")) return;
    state.loading = true;
    message("");
    updateControls();
    try {
      const data = await api("/api/rules");
      if (!Array.isArray(data.rules) || typeof data.version !== "string") throw new Error("词库数据格式不正确，未载入文件。");
      state.rules = data.rules;
      state.version = data.version;
      state.dirty = false;
      state.conflict = false;
      state.readError = false;
      state.revision += 1;
      nodes["backup-status"].textContent = "";
      nodes["test-result"].hidden = true;
    } catch (error) {
      if (error.status !== 401) state.readError = true;
      message(error.message, "error");
    } finally {
      state.loading = false;
      renderRules();
      updateControls();
    }
  }

  async function saveRules() {
    if (state.saving || state.loading || !state.dirty || state.conflict || state.readError || state.rules === null) return;
    state.saving = true;
    message("");
    updateControls();
    try {
      const data = await api("/api/rules", { method: "PUT", body: { rules: state.rules, version: state.version } });
      if (!Array.isArray(data.rules) || typeof data.version !== "string") throw new Error("保存返回内容不正确，请重新读取并核对文件。");
      state.rules = data.rules;
      state.version = data.version;
      state.dirty = false;
      state.revision += 1;
      nodes["backup-status"].textContent = data.backup ? "上次备份：" + String(data.backup).split(/[\\/]/).pop() : "";
      nodes["test-result"].hidden = true;
      message(data.warning ? "词库已保存。" + data.warning : "词库已保存。小爱下次处理指令时就会使用新内容。", data.warning ? "warning" : "info");
    } catch (error) {
      if (error.status === 409) state.conflict = true;
      message(error.message, "error");
    } finally {
      state.saving = false;
      renderRules();
      updateControls();
    }
  }

  function editRule(index = null) {
    if (state.saving || state.loading || state.readError || state.rules === null) return;
    state.editingIndex = index;
    const rule = index === null ? { name: "", keywords: [], reply: "", repeat: 1 } : state.rules[index];
    nodes["edit-title"].textContent = index === null ? "添加规则" : "编辑规则";
    nodes["rule-name"].value = rule.name;
    nodes["rule-keywords"].value = rule.keywords.join("\n");
    nodes["rule-reply"].value = rule.reply;
    nodes["rule-repeat"].value = rule.repeat;
    nodes["edit-error"].hidden = true;
    nodes["edit-dialog"].showModal();
    nodes["rule-name"].focus();
  }

  function closeEditor() { nodes["edit-dialog"].close(); }

  function submitEdit(event) {
    event.preventDefault();
    const index = state.editingIndex;
    const original = index === null ? null : state.rules[index];
    const keywords = original && nodes["rule-keywords"].value === original.keywords.join("\n") ? original.keywords : nodes["rule-keywords"].value.split(/\r?\n/).filter((word) => word.trim().length > 0);
    const name = nodes["rule-name"].value;
    const reply = nodes["rule-reply"].value;
    const repeat = Number(nodes["rule-repeat"].value);
    if (!name.trim() || !keywords.length || !reply.trim() || !Number.isSafeInteger(repeat) || repeat < 1) {
      nodes["edit-error"].textContent = "请填写名称、至少一个关键词和回复；重复次数需要是正整数。";
      nodes["edit-error"].hidden = false;
      return;
    }
    const rule = { ...(original || {}), name, keywords, reply, repeat };
    if (index === null) state.rules.push(rule);
    else state.rules[index] = rule;
    closeEditor();
    markDirty();
  }

  function moveRule(index, offset) {
    if (state.saving || state.loading || state.readError || state.rules === null) return;
    const target = index + offset;
    if (target < 0 || target >= state.rules.length) return;
    [state.rules[index], state.rules[target]] = [state.rules[target], state.rules[index]];
    markDirty();
  }

  function deleteRule(index) {
    if (state.saving || state.loading || state.readError || state.rules === null) return;
    if (!window.confirm("删除「" + state.rules[index].name + "」？删除后还需要点击「保存词库」才会写入文件。")) return;
    state.rules.splice(index, 1);
    markDirty();
  }

  async function testQuery(event) {
    event.preventDefault();
    if (state.testing || state.saving || state.loading || state.readError || state.rules === null) return;
    const query = nodes["test-query"].value;
    if (!query) return;
    state.testing = true;
    nodes["test-result"].hidden = true;
    updateControls();
    const testWasDirty = state.dirty;
    const testRevision = state.revision;
    try {
      const body = { query };
      if (testWasDirty) body.rules = state.rules;
      const data = await api("/api/test", { method: "POST", body });
      if (testRevision !== state.revision) return;
      const result = nodes["test-result"];
      result.replaceChildren();
      result.className = "test-result" + (data.matched ? "" : " no-match");
      result.append(element("p", "result-status", data.matched ? "✓ 命中规则" : "没有命中规则"));
      if (data.matched) {
        result.append(element("strong", "", "第 " + (data.index + 1) + " 条 · " + data.rule.name), element("p", "result-label", "命中关键词"), element("p", "", data.keyword), element("p", "result-label", "回复内容"), element("p", "result-reply", data.reply), element("p", "result-label", "重复 " + data.repeat + " 次"));
      } else result.append(element("p", "", "可以把小爱识别出来的词加入相关规则的关键词中。"));
      if (testWasDirty) result.append(element("p", "result-label", "本次使用未保存的词库"));
      result.hidden = false;
    } catch (error) {
      const result = nodes["test-result"];
      result.replaceChildren(element("p", "error-text", error.message));
      result.className = "test-result";
      result.hidden = false;
    } finally { state.testing = false; updateControls(); }
  }

  async function login(event) {
    event.preventDefault();
    if (state.loginBusy) return;
    state.loginBusy = true;
    nodes["login-button"].disabled = true;
    nodes["login-button"].textContent = "正在进入…";
    nodes["login-error"].hidden = true;
    try {
      const data = await api("/api/login", { method: "POST", body: { password: nodes.password.value } });
      if (!data.authenticated || typeof data.csrfToken !== "string") throw new Error("登录返回内容不正确。");
      state.authenticated = true;
      state.csrfToken = data.csrfToken;
      nodes.password.value = "";
      showSession();
      if (state.rules === null) await loadRules(false);
      else { message(state.dirty ? "已重新登录，未保存修改仍保留在页面。保存时会核对文件版本。" : "已重新登录。点击「重新读取」可获取最新词库。"); renderRules(); }
    } catch (error) {
      nodes["login-error"].textContent = error.message;
      nodes["login-error"].hidden = false;
    } finally {
      state.loginBusy = false;
      nodes["login-button"].disabled = false;
      nodes["login-button"].textContent = "进入词库 →";
    }
  }

  async function logout() {
    if (state.saving || state.loading) return;
    if (state.dirty && !window.confirm("退出会放弃页面中未保存的修改。确定退出吗？")) return;
    try {
      await api("/api/logout", { method: "POST", body: {} });
      state.authenticated = false;
      state.csrfToken = "";
      state.rules = null;
      state.version = null;
      state.dirty = false;
      state.conflict = false;
      state.readError = false;
      state.revision += 1;
      nodes["search-input"].value = "";
      nodes["test-query"].value = "";
      showSession();
      nodes.password.focus();
    } catch (error) { message(error.message, "error"); }
  }

  async function init() {
    try {
      const data = await api("/api/session");
      state.authenticated = data.authenticated === true;
      state.csrfToken = data.csrfToken || "";
      showSession();
      if (state.authenticated) await loadRules(false);
      else nodes.password.focus();
    } catch (error) {
      showSession();
      nodes["login-error"].textContent = "连接失败：" + error.message;
      nodes["login-error"].hidden = false;
    }
  }

  $("login-form").addEventListener("submit", login);
  nodes["logout-button"].addEventListener("click", logout);
  nodes["reload-button"].addEventListener("click", () => loadRules());
  $("conflict-reload-button").addEventListener("click", () => loadRules());
  nodes["save-button"].addEventListener("click", saveRules);
  nodes["add-button"].addEventListener("click", () => editRule());
  nodes["search-input"].addEventListener("input", renderRules);
  $("test-form").addEventListener("submit", testQuery);
  $("edit-form").addEventListener("submit", submitEdit);
  $("close-dialog-button").addEventListener("click", closeEditor);
  $("cancel-dialog-button").addEventListener("click", closeEditor);
  window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
  init();
})();
