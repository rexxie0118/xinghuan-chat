/* 星环 Chat — vanilla ES2020 front end. No build step, no external deps. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    meta: null,
    user: null,
    conversations: [],
    activeId: null,
    kbs: [],
    kbId: null,
    defaultKb: null,
    agents: [],
    agentId: null,
    messages: [],
    mode: "agent",
    live: false,
    sending: false,
    citations: [],
    graph: null,
    graphQuery: false,
    graphTransform: { x: 0, y: 0, k: 1 },
    overview: null,
    panelView: "graph",
    search: "",
    authView: "login",
  };

  const TYPE_COLORS = {
    项目: "#8ca6ff",
    人物: "#67e8d5",
    系统: "#f4c47a",
    数据: "#c4a7ff",
    组织: "#ff9f9f",
    文档: "#82c8ff",
    流程: "#a7e38a",
    决策: "#ffb86b",
    事件: "#ff8fb1",
    概念: "#9aa7bd",
  };

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data;
    }
  }

  /* --------------------------------------------------------------- api */
  async function api(path, opts = {}) {
    const headers = { Accept: "application/json", ...(opts.headers || {}) };
    let body = opts.body;
    if (body && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, {
        ...opts,
        body,
        headers,
        credentials: "same-origin",
      });
    } catch (err) {
      throw new ApiError("网络连接失败，请确认服务仍在运行", 0, null);
    }
    let data = null;
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text.slice(0, 300) };
      }
    }
    if (!res.ok) {
      let message = "请求失败";
      if (data && Array.isArray(data.detail)) {
        message = data.detail.map((d) => d.msg || String(d)).join("；");
      } else if (data && data.detail) {
        message = String(data.detail);
      } else if (res.status === 401) {
        message = "请先登录";
      }
      throw new ApiError(message, res.status, data);
    }
    return data;
  }

  /* ------------------------------------------------------------- utils */
  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function truncate(text, n) {
    const value = String(text ?? "");
    return value.length > n ? value.slice(0, n - 1) + "…" : value;
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  }

  function timeLabel(isoString) {
    if (!isoString) return "";
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return "";
    const now = new Date();
    const sameDay = date.toDateString() === now.toDateString();
    return sameDay
      ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  }

  function dayGroup(isoString) {
    if (!isoString) return "更早";
    const date = new Date(isoString);
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startYesterday = new Date(startToday.getTime() - 86400000);
    if (date >= startToday) return "今天";
    if (date >= startYesterday) return "昨天";
    return "更早";
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const el = $("#toast");
    el.textContent = message;
    el.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.className = "toast" + (kind ? " " + kind : "");
    }, 3200);
  }

  function showError(el, message) {
    el.textContent = message;
    el.hidden = false;
  }

  /* ------------------------------------------------------------ markdown */
  function renderInline(raw) {
    let text = escapeHtml(raw);
    text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    text = text.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    text = text.replace(
      /\[(\d{1,2})\]/g,
      '<button type="button" class="citation-ref" data-cite="$1" title="查看引用 $1">$1</button>'
    );
    return text;
  }

  function renderMarkdown(source) {
    const text = String(source || "").replace(/\r\n/g, "\n");
    const lines = text.split("\n");
    let html = "";
    let paragraph = [];
    let listType = null;
    let code = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      html += `<p>${renderInline(paragraph.join(" "))}</p>`;
      paragraph = [];
    };
    const closeList = () => {
      if (listType) {
        html += `</${listType}>`;
        listType = null;
      }
    };
    const openList = (type) => {
      if (listType === type) return;
      closeList();
      html += `<${type}>`;
      listType = type;
    };

    for (const line of lines) {
      if (code !== null) {
        if (/^```/.test(line.trim())) {
          html += `<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`;
          code = null;
        } else {
          code.push(line);
        }
        continue;
      }
      if (/^```/.test(line.trim())) {
        flushParagraph();
        closeList();
        code = [];
        continue;
      }
      if (!line.trim()) {
        flushParagraph();
        closeList();
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        flushParagraph();
        closeList();
        const level = heading[1].length;
        html += `<h${level}>${renderInline(heading[2])}</h${level}>`;
        continue;
      }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
        flushParagraph();
        closeList();
        html += "<hr>";
        continue;
      }
      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph();
        closeList();
        html += `<blockquote>${renderInline(quote[1])}</blockquote>`;
        continue;
      }
      const ul = line.match(/^\s*[-*+]\s+(.+)$/);
      if (ul) {
        flushParagraph();
        openList("ul");
        html += `<li>${renderInline(ul[1])}</li>`;
        continue;
      }
      const ol = line.match(/^\s*\d+\.\s+(.+)$/);
      if (ol) {
        flushParagraph();
        openList("ol");
        html += `<li>${renderInline(ol[1])}</li>`;
        continue;
      }
      closeList();
      paragraph.push(line.trim());
    }
    if (code !== null) {
      html += `<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`;
    }
    flushParagraph();
    closeList();
    return html || "<p></p>";
  }

  /* --------------------------------------------------------------- auth */
  function applyTheme(theme) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("webchat-theme", next);
    } catch {
      /* private mode may block storage */
    }
    const label = $(".theme-label");
    if (label) label.textContent = next === "light" ? "切换深色" : "切换浅色";
  }

  function showAuth() {
    state.user = null;
    state.activeId = null;
    state.messages = [];
    state.sending = false;
    $("#auth-screen").hidden = false;
    $("#app-shell").classList.add("is-hidden");
    document.title = "星环 Chat · 登录";
    $("#login-username").focus({ preventScroll: true });
  }

  function showApp() {
    $("#auth-screen").hidden = true;
    $("#app-shell").classList.remove("is-hidden");
  }

  function setAuthView(view) {
    state.authView = view;
    $$(".auth-tab").forEach((tab) => {
      const active = tab.dataset.authView === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $("#login-form").hidden = view !== "login";
    $("#register-form").hidden = view !== "register";
    $("#auth-title").textContent = view === "login" ? "登录星环" : "创建账号";
    const error = view === "login" ? $("#login-error") : $("#register-error");
    error.hidden = true;
  }

  async function handleLogin(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    const errorEl = $("#login-error");
    errorEl.hidden = true;
    button.disabled = true;
    try {
      const data = await api("/api/auth/login", {
        method: "POST",
        body: {
          username: $("#login-username").value.trim(),
          password: $("#login-password").value,
        },
      });
      await enterApp(data.user);
      toast(`欢迎回来，${data.user.display_name}`, "success");
    } catch (err) {
      showError(errorEl, err.message);
    } finally {
      button.disabled = false;
    }
  }

  async function handleRegister(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    const errorEl = $("#register-error");
    errorEl.hidden = true;
    button.disabled = true;
    try {
      const data = await api("/api/auth/register", {
        method: "POST",
        body: {
          username: $("#register-username").value.trim(),
          display_name: $("#register-display").value.trim() || null,
          department: $("#register-department").value,
          password: $("#register-password").value,
        },
      });
      await enterApp(data.user);
      toast("账号已创建，欢迎加入", "success");
    } catch (err) {
      showError(errorEl, err.message);
    } finally {
      button.disabled = false;
    }
  }

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      /* cookie will expire eventually */
    }
    state.conversations = [];
    state.kbs = [];
    state.kbId = null;
    state.agents = [];
    state.agentId = null;
    state.overview = null;
    state.graph = null;
    state.globalGraph = null;
    state.citations = [];
    $("#messages").innerHTML = "";
    $("#conversation-list").innerHTML = "";
    showAuth();
    toast("已退出登录");
  }

  /* ------------------------------------------------------------- boot */
  async function boot() {
    let savedTheme = "dark";
    try {
      savedTheme = localStorage.getItem("webchat-theme") || "dark";
    } catch {
      savedTheme = "dark";
    }
    applyTheme(savedTheme);
    bindEvents();
    await loadMeta();
    try {
      const data = await api("/api/auth/me");
      await enterApp(data.user);
    } catch (err) {
      showAuth();
      if (err.status && err.status !== 401) toast(err.message, "error");
    }
  }

  async function loadMeta() {
    try {
      state.meta = await api("/api/meta");
    } catch {
      state.meta = {
        name: "星环 Chat",
        agent_enabled: false,
        kb: { provider: "mock", warning: null, corpus: {} },
        departments: ["产品部", "研发部", "人事部"],
        default_department: "产品部",
        knowledge_bases: [],
        demo_accounts: [
          { username: "demo", password: "demo1234", display_name: "演示用户", department: "产品部" },
          { username: "alice", password: "alice1234", display_name: "Alice", department: "研发部" },
          { username: "bob", password: "bob1234", display_name: "Bob", department: "人事部" },
        ],
      };
    }
    renderDemoAccounts();
    renderDepartmentOptions();
    renderEngineBadge();
  }

  function renderDepartmentOptions() {
    const select = $("#register-department");
    if (!select) return;
    select.innerHTML = "";
    const departments = state.meta?.departments || ["产品部", "研发部", "人事部"];
    for (const department of departments) {
      const option = document.createElement("option");
      option.value = department;
      option.textContent = department;
      select.append(option);
    }
    const preferred = state.meta?.default_department;
    if (preferred && departments.includes(preferred)) select.value = preferred;
  }

  async function loadKbCatalog() {
    const data = await api("/api/kbs");
    state.kbs = data.knowledge_bases || [];
    state.defaultKb = data.default || state.kbs[0]?.id || null;
    if (!state.kbs.length) {
      state.kbId = null;
      updateKbSelector();
      return;
    }
    let saved = null;
    try {
      saved = localStorage.getItem("webchat-kb");
    } catch {
      saved = null;
    }
    state.kbId = state.kbs.some((kb) => kb.id === saved)
      ? saved
      : state.defaultKb;
    updateKbSelector();
  }

  function currentKb() {
    return state.kbs.find((kb) => kb.id === state.kbId) || null;
  }

  function kbLabel(department) {
    return department === "all" ? "公司共享" : department;
  }

  function updateKbSelector() {
    const kb = currentKb();
    const name = $("#kb-current-name");
    const dept = $("#kb-current-dept");
    const dot = $("#kb-current-dot");
    if (!kb) {
      name.textContent = "没有可用知识库";
      dept.textContent = "—";
      return;
    }
    name.textContent = kb.name;
    dept.textContent = kbLabel(kb.department);
    dot.style.background = kb.color || "var(--accent)";
    dot.style.boxShadow = `0 0 0 3px color-mix(in srgb, ${kb.color || "#8ca6ff"} 16%, transparent)`;
    const label = $("#empty-kb-label");
    if (label) label.textContent = `当前知识库 · ${kb.name} · ${kbLabel(kb.department)}`;
    renderKbMenu();
  }

  function renderKbMenu() {
    const menu = $("#kb-menu");
    if (!menu) return;
    menu.innerHTML = "";
    if (!state.kbs.length) {
      menu.innerHTML = `<div class="kb-menu-empty">当前账号没有可访问的知识库</div>`;
      return;
    }
    const groups = new Map();
    for (const kb of state.kbs) {
      const key = kb.department === "all" ? "公司共享" : kb.department;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(kb);
    }
    for (const [group, items] of groups) {
      const label = document.createElement("div");
      label.className = "kb-menu-label";
      label.textContent = group;
      menu.append(label);
      for (const kb of items) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "kb-menu-item" + (kb.id === state.kbId ? " active" : "");
        btn.setAttribute("role", "option");
        btn.setAttribute("aria-selected", String(kb.id === state.kbId));
        btn.innerHTML = `
          <span class="kb-item-dot" style="background:${escapeHtml(kb.color || "#8ca6ff")}"></span>
          <span class="kb-item-meta">
            <span class="kb-item-name"></span>
            <span class="kb-item-desc"></span>
            <span class="kb-item-count">${formatNumber(kb.documents)} 篇文档 · ${formatNumber(kb.entities)} 个实体</span>
          </span>
          ${kb.id === state.kbId ? '<span class="kb-item-check"><svg class="icon"><use href="#i-check"/></svg></span>' : ""}
        `;
        btn.querySelector(".kb-item-name").textContent = kb.name;
        btn.querySelector(".kb-item-desc").textContent = kb.description || "";
        btn.addEventListener("click", (event) => {
          event.stopPropagation();
          switchKnowledgeBase(kb.id);
        });
        menu.append(btn);
      }
    }
  }

  async function switchKnowledgeBase(kbId) {
    if (!kbId || kbId === state.kbId) {
      closeKbMenu();
      return;
    }
    if (state.sending) {
      toast("回答生成中，请稍候再切换知识库");
      return;
    }
    state.kbId = kbId;
    try {
      localStorage.setItem("webchat-kb", kbId);
    } catch {
      /* storage may be blocked */
    }
    closeKbMenu();
    state.activeId = null;
    state.messages = [];
    state.citations = [];
    state.search = "";
    $("#conversation-search").value = "";
    state.graph = null;
    state.globalGraph = null;
    updateKbSelector();
    showEmptyState();
    updateHeader(null);
    renderConversations();
    await Promise.all([loadOverview(), loadConversations(), loadGlobalGraph()]);
    if (state.conversations.length) {
      await selectConversation(state.conversations[0].id, { silent: true });
    } else {
      showEmptyState();
      updateHeader(null);
    }
    toast(`已切换到「${currentKb()?.name || "知识库"}」`);
  }

  function closeKbMenu() {
    $("#kb-menu").hidden = true;
    $("#kb-switcher").setAttribute("aria-expanded", "false");
  }

  function toggleKbMenu() {
    const menu = $("#kb-menu");
    const open = menu.hidden;
    renderKbMenu();
    menu.hidden = !open;
    $("#kb-switcher").setAttribute("aria-expanded", String(open));
  }

  async function loadAgents() {
    try {
      const data = await api("/api/agents");
      state.agents = data.agents || [];
    } catch (err) {
      state.agents = [];
      if (err.status !== 401) toast("Agent 列表加载失败：" + err.message, "error");
    }
    let saved = null;
    try {
      saved = localStorage.getItem("webchat-agent");
    } catch {
      saved = null;
    }
    state.agentId = state.agents.some((agent) => agent.id === saved) ? saved : null;
    updateAgentPicker();
  }

  function currentAgent() {
    return state.agents.find((agent) => agent.id === state.agentId) || null;
  }

  function updateAgentPicker() {
    const picker = $("#agent-picker");
    if (!picker) return;
    picker.hidden = state.mode !== "agent";
    const agent = currentAgent();
    $("#agent-picker-label").textContent = agent ? agent.name : "默认 Agent";
    picker.classList.toggle("has-agent", Boolean(agent));
    renderAgentMenu();
    renderAgentSubmenu();
  }

  function agentKind(agent) {
    if (agent?.pipeline) return "Pipeline";
    return agent?.kind === "skill" ? "Skill" : "Agent";
  }

  function renderAgentSubmenu() {
    const menu = $("#agent-submenu");
    if (!menu) return;
    $("#agent-nav-count").textContent = String(state.agents.length);
    menu.innerHTML = "";
    const defaultItem = document.createElement("button");
    defaultItem.type = "button";
    defaultItem.className = "agent-submenu-item" + (!state.agentId ? " active" : "");
    defaultItem.innerHTML = `
      <span class="agent-menu-icon"><svg class="icon"><use href="#i-sparkle"/></svg></span>
      <span class="agent-submenu-meta">
        <span class="agent-submenu-name">默认 Agent</span>
        <span class="agent-submenu-desc">内置 / 外部 Agent 服务</span>
      </span>
    `;
    defaultItem.addEventListener("click", () => {
      setMode("agent");
      selectAgent(null);
      closeAgentNav();
      closeMobileSidebar();
    });
    menu.append(defaultItem);

    for (const agent of state.agents) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "agent-submenu-item" + (agent.id === state.agentId ? " active" : "");
      item.innerHTML = `
        <span class="agent-menu-icon"><svg class="icon"><use href="#i-terminal"/></svg></span>
        <span class="agent-submenu-meta">
          <span class="agent-submenu-name"></span>
          <span class="agent-submenu-desc"></span>
        </span>
        <span class="agent-submenu-kind ${agent.kind === "skill" ? "" : "agent"}">${agentKind(agent)}</span>
      `;
      item.querySelector(".agent-submenu-name").textContent = agent.name;
      item.querySelector(".agent-submenu-desc").textContent =
        `${agent.files?.length || 0} 个文件 · ${agent.entrypoint || "无入口脚本"}`;
      item.addEventListener("click", () => {
        setMode("agent");
        selectAgent(agent.id);
        closeAgentNav();
        closeMobileSidebar();
      });
      menu.append(item);
    }

    if (!state.agents.length) {
      const empty = document.createElement("div");
      empty.className = "agent-submenu-empty";
      empty.textContent = "还没有导入 Agent / Skill。可以从 GitHub 或本地文件导入。";
      menu.append(empty);
    }

    const actions = document.createElement("div");
    actions.className = "agent-submenu-actions";
    const manage = document.createElement("button");
    manage.type = "button";
    manage.className = "agent-submenu-action";
    manage.innerHTML = `<svg class="icon"><use href="#i-panel"/></svg><span>管理</span>`;
    manage.addEventListener("click", () => {
      closeAgentNav();
      openAgentManager();
    });
    const github = document.createElement("button");
    github.type = "button";
    github.className = "agent-submenu-action";
    github.innerHTML = `<svg class="icon"><use href="#i-arrow-right"/></svg><span>GitHub 导入</span>`;
    github.addEventListener("click", () => {
      closeAgentNav();
      openAgentManager({ focusGithub: true });
    });
    actions.append(manage, github);
    menu.append(actions);
  }

  function toggleAgentNav() {
    const menu = $("#agent-submenu");
    const toggle = $("#agent-nav-toggle");
    const open = menu.hidden;
    renderAgentSubmenu();
    menu.hidden = !open;
    toggle.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
  }

  function closeAgentNav() {
    $("#agent-submenu").hidden = true;
    $("#agent-nav-toggle").classList.remove("open");
    $("#agent-nav-toggle").setAttribute("aria-expanded", "false");
  }

  function renderAgentMenu() {
    const menu = $("#agent-menu");
    if (!menu) return;
    menu.innerHTML = "";
    const label = document.createElement("div");
    label.className = "agent-menu-label";
    label.textContent = "选择执行方式";
    menu.append(label);

    const defaultItem = document.createElement("button");
    defaultItem.type = "button";
    defaultItem.className = "agent-menu-item" + (!state.agentId ? " active" : "");
    defaultItem.innerHTML = `
      <span class="agent-menu-icon"><svg class="icon"><use href="#i-sparkle"/></svg></span>
      <span class="agent-menu-meta">
        <span class="agent-menu-name">默认 Agent</span>
        <span class="agent-menu-desc">检索 + 图谱 + 内置/外部 Agent 服务</span>
      </span>
      ${!state.agentId ? '<span class="agent-menu-check"><svg class="icon"><use href="#i-check"/></svg></span>' : ""}
    `;
    defaultItem.addEventListener("click", () => selectAgent(null));
    menu.append(defaultItem);

    for (const agent of state.agents) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "agent-menu-item" + (agent.id === state.agentId ? " active" : "");
      item.innerHTML = `
        <span class="agent-menu-icon"><svg class="icon"><use href="#i-terminal"/></svg></span>
        <span class="agent-menu-meta">
          <span class="agent-menu-name"></span>
          <span class="agent-menu-desc"></span>
        </span>
        ${agent.id === state.agentId ? '<span class="agent-menu-check"><svg class="icon"><use href="#i-check"/></svg></span>' : ""}
      `;
      item.querySelector(".agent-menu-name").textContent = agent.name;
      item.querySelector(".agent-menu-desc").textContent =
        `${agentKind(agent)} · ${agent.files?.length || 0} 个文件 · `
        + (agent.entrypoint || (agent.kind === "skill" ? "Prompt Skill（需要模型）" : "无入口脚本"))
        + (agent.source_url ? ` · ${agent.source_url}` : "");
      item.addEventListener("click", () => selectAgent(agent.id));
      menu.append(item);
    }

    const foot = document.createElement("div");
    foot.className = "agent-menu-foot";
    const importBtn = document.createElement("button");
    importBtn.type = "button";
    importBtn.className = "agent-menu-action";
    importBtn.innerHTML = `<svg class="icon"><use href="#i-plus"/></svg><span>导入 Agent</span>`;
    importBtn.addEventListener("click", openAgentManager);
    const manageBtn = document.createElement("button");
    manageBtn.type = "button";
    manageBtn.className = "agent-menu-action";
    manageBtn.innerHTML = `<svg class="icon"><use href="#i-panel"/></svg><span>管理</span>`;
    manageBtn.addEventListener("click", openAgentManager);
    foot.append(importBtn, manageBtn);
    menu.append(foot);
  }

  function selectAgent(agentId, opts = {}) {
    if (agentId && !state.agents.some((agent) => agent.id === agentId)) return;
    state.agentId = agentId;
    try {
      if (agentId) localStorage.setItem("webchat-agent", agentId);
      else localStorage.removeItem("webchat-agent");
    } catch {
      /* storage may be blocked */
    }
    updateAgentPicker();
    closeAgentMenu();
    if (!opts.silent) {
      const agent = currentAgent();
      toast(agent ? `已选择 ${agentKind(agent)}「${agent.name}」` : "已切换到默认 Agent");
    }
  }

  function closeAgentMenu() {
    const menu = $("#agent-menu");
    if (!menu) return;
    menu.hidden = true;
    $("#agent-picker").setAttribute("aria-expanded", "false");
  }

  function toggleAgentMenu() {
    if (state.mode !== "agent") return;
    const menu = $("#agent-menu");
    const open = menu.hidden;
    renderAgentMenu();
    menu.hidden = !open;
    $("#agent-picker").setAttribute("aria-expanded", String(open));
  }

  async function enterApp(user) {
    state.user = user;
    state.activeId = null;
    state.messages = [];
    state.conversations = [];
    state.kbs = [];
    state.kbId = null;
    state.agents = [];
    state.agentId = null;
    state.graph = null;
    state.globalGraph = null;
    state.overview = null;
    state.citations = [];
    showApp();
    $("#user-name").textContent = user.display_name || user.username;
    $("#user-subtitle").textContent = `${user.department || "未分组"} · ${user.role || "member"}`;
    const adminBtn = $("#admin-btn");
    if (adminBtn) adminBtn.hidden = user.role !== "admin";
    $("#user-avatar").textContent = (user.display_name || user.username).slice(0, 1).toUpperCase();
    document.title = "星环 Chat";
    try {
      await Promise.all([loadKbCatalog(), loadAgents()]);
    } catch (err) {
      toast("知识库目录加载失败：" + err.message, "error");
      return;
    }
    if (!state.kbId) {
      toast("当前账号还没有可访问的知识库，请联系管理员", "error");
      return;
    }
    resetEmptyState();
    await Promise.all([loadOverview(), loadConversations(), loadGlobalGraph()]);
    if (state.conversations.length) {
      await selectConversation(state.conversations[0].id, { silent: true });
    } else {
      showEmptyState();
      updateHeader(null);
    }
    syncPanelMode();
    $("#message-input").focus({ preventScroll: true });
  }

  /* ------------------------------------------------------------- meta UI */
  function renderDemoAccounts() {
    const wrap = $("#demo-accounts");
    wrap.innerHTML = "";
    const accounts = state.meta?.demo_accounts || [];
    for (const account of accounts) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "demo-chip";
      const strong = document.createElement("strong");
      strong.textContent = `${account.display_name || account.username}${account.department ? ` · ${account.department}` : ""}`;
      const span = document.createElement("span");
      span.textContent = `${account.username} / ${account.password}`;
      btn.append(strong, span);
      btn.addEventListener("click", () => {
        setAuthView("login");
        $("#login-username").value = account.username;
        $("#login-password").value = account.password;
        $("#login-password").focus();
      });
      wrap.append(btn);
    }
  }

  function renderEngineBadge() {
    const badge = $("#engine-badge");
    const external = Boolean(state.meta?.agent_enabled);
    badge.classList.toggle("external", external);
    badge.querySelector("span:last-child").textContent = external ? "外部 Agent" : "本地 Agent";
  }

  async function loadOverview() {
    if (!state.kbId) return;
    try {
      state.overview = await api(`/api/kb/overview?kb=${encodeURIComponent(state.kbId)}`);
    } catch (err) {
      state.overview = null;
      toast("知识库概览加载失败：" + err.message, "error");
      return;
    }
    const stats = state.overview.stats || {};
    $("#stat-docs").textContent = formatNumber(stats.documents);
    $("#stat-entities").textContent = formatNumber(stats.entities);
    $("#stat-relations").textContent = formatNumber(stats.relations);
    $("#doc-count").textContent = formatNumber((state.overview.documents || []).length);
    $("#kb-summary").innerHTML = "";
    const summary = $("#kb-summary");
    const items = [
      [stats.documents, "篇文档"],
      [stats.entities, "个实体"],
      [stats.relations, "条关系"],
    ];
    items.forEach(([value, label], index) => {
      if (index) {
        const sep = document.createElement("span");
        sep.className = "dot-sep";
        summary.append(sep);
      }
      const span = document.createElement("span");
      const b = document.createElement("b");
      b.textContent = formatNumber(value);
      span.append(b, ` ${label}`);
      summary.append(span);
    });
    const hint = document.createElement("span");
    hint.className = "summary-hint";
    hint.textContent = "全部来自本地知识库";
    summary.append(hint);
    renderSuggestions();
    renderDocs();
    renderProvider();
  }

  function renderSuggestions() {
    const grid = $("#suggestion-grid");
    grid.innerHTML = "";
    const questions = state.overview?.sample_questions || [
      "北极星项目的负责人是谁？",
      "统一登录服务依赖什么？",
      "为什么放弃自建 MQ？",
      "外部 Agent 怎么接入？",
    ];
    const tags = [
      ["人物", "#i-user"],
      ["架构", "#i-graph"],
      ["决策", "#i-list"],
      ["Agent", "#i-sparkle"],
      ["安全", "#i-lock"],
    ];
    questions.slice(0, 4).forEach((question, index) => {
      const [tag, icon] = tags[index % tags.length];
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "suggestion-card";
      btn.innerHTML = `
        <span class="suggestion-tag"><svg class="icon"><use href="${icon}"/></svg>${escapeHtml(tag)}</span>
        <span class="suggestion-q">${escapeHtml(question)}</span>
        <span class="suggestion-arrow"><svg class="icon"><use href="#i-arrow-right"/></svg></span>
      `;
      btn.addEventListener("click", () => askQuestion(question));
      grid.append(btn);
    });
  }

  function renderDocs() {
    const list = $("#doc-list");
    list.innerHTML = "";
    const docs = state.overview?.documents || [];
    if (!docs.length) {
      list.innerHTML = `<div class="docs-empty"><svg class="icon"><use href="#i-book"/></svg><div>还没有摄取文档</div></div>`;
      return;
    }
    for (const doc of docs) {
      const item = document.createElement("div");
      item.className = "doc-item";
      item.innerHTML = `
        <span class="doc-icon"><svg class="icon"><use href="#i-book"/></svg></span>
        <span class="doc-meta"><strong></strong><span>${escapeHtml(doc.doc_type || "document")}</span></span>
        <span class="doc-chunks">${formatNumber(doc.chunks)} 段</span>
      `;
      item.querySelector("strong").textContent = doc.title;
      list.append(item);
    }
    const samples = $("#sample-questions");
    samples.innerHTML = "";
    for (const question of (state.overview?.sample_questions || []).slice(0, 4)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sample-question";
      btn.innerHTML = `<svg class="icon"><use href="#i-arrow-right"/></svg><span></span>`;
      btn.querySelector("span").textContent = question;
      btn.addEventListener("click", () => askQuestion(question));
      samples.append(btn);
    }
  }

  function renderProvider() {
    const provider = state.overview?.provider || state.meta?.kb || {};
    $("#provider-label").textContent =
      provider.kind === "mock" ? "本地混合检索 · 离线模式" : `检索后端 · ${provider.kind}`;
    $("#provider-chat").textContent = provider.chat_model || "unknown";
    $("#provider-embed").textContent = provider.embedding_model || "unknown";
    const warning = $("#provider-warning");
    if (provider.warning) {
      warning.textContent = provider.warning;
      warning.hidden = false;
    } else {
      warning.hidden = true;
    }
  }

  /* --------------------------------------------------------- conversations */
  async function loadConversations() {
    if (!state.kbId) return;
    try {
      const data = await api(`/api/conversations?kb=${encodeURIComponent(state.kbId)}`);
      state.conversations = data.conversations || [];
    } catch (err) {
      if (err.status === 401) return showAuth();
      toast("会话列表加载失败：" + err.message, "error");
      state.conversations = [];
    }
    renderConversations();
  }

  function renderConversations() {
    const list = $("#conversation-list");
    list.innerHTML = "";
    const query = state.search.trim().toLowerCase();
    const filtered = state.conversations.filter((conv) => {
      if (!query) return true;
      return (
        conv.title.toLowerCase().includes(query) ||
        (conv.last_message || "").toLowerCase().includes(query)
      );
    });
    $("#conversation-count").textContent = String(state.conversations.length);
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "list-empty";
      empty.textContent = query ? "没有匹配的会话" : "还没有对话。\n从一个问题开始吧。";
      empty.style.whiteSpace = "pre-line";
      list.append(empty);
      return;
    }
    const groups = {};
    for (const conv of filtered) {
      const group = dayGroup(conv.updated_at);
      (groups[group] ||= []).push(conv);
    }
    for (const groupName of ["今天", "昨天", "更早"]) {
      if (!groups[groupName]?.length) continue;
      const group = document.createElement("div");
      group.className = "conv-group";
      const label = document.createElement("div");
      label.className = "conv-group-label";
      label.textContent = groupName;
      group.append(label);
      for (const conv of groups[groupName]) group.append(buildConversationItem(conv));
      list.append(group);
    }
  }

  function buildConversationItem(conv) {
    const item = document.createElement("div");
    item.className = "conv-item" + (conv.id === state.activeId ? " active" : "");
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.dataset.id = conv.id;
    item.innerHTML = `
      <span class="conv-content">
        <span class="conv-title"></span>
        <span class="conv-preview"></span>
      </span>
      <span class="conv-actions">
        <button type="button" class="conv-action" data-action="rename" title="重命名" aria-label="重命名">
          <svg class="icon"><use href="#i-edit"/></svg>
        </button>
        <button type="button" class="conv-action danger" data-action="delete" title="删除" aria-label="删除">
          <svg class="icon"><use href="#i-trash"/></svg>
        </button>
      </span>
    `;
    item.querySelector(".conv-title").textContent = conv.title;
    item.querySelector(".conv-preview").textContent = conv.last_message || "等待第一个问题…";
    item.addEventListener("click", (event) => {
      if (event.target.closest(".conv-action")) return;
      selectConversation(conv.id);
    });
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectConversation(conv.id);
      }
    });
    item.querySelector('[data-action="rename"]').addEventListener("click", (event) => {
      event.stopPropagation();
      renameConversation(conv);
    });
    item.querySelector('[data-action="delete"]').addEventListener("click", (event) => {
      event.stopPropagation();
      deleteConversation(conv);
    });
    return item;
  }

  async function selectConversation(id, opts = {}) {
    if (state.sending) {
      toast("回答生成中，请稍候");
      return;
    }
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
      state.activeId = id;
      state.messages = data.messages || [];
      state.mode = data.conversation.mode === "rag" ? "rag" : "agent";
      renderMessages();
      updateHeader(data.conversation);
      renderConversations();
      setMode(state.mode, { silent: true });
      if (!opts.silent) closeMobileSidebar();
      if (data.conversation.title) document.title = `${data.conversation.title} · 星环 Chat`;
    } catch (err) {
      if (err.status === 404) {
        toast("会话不存在或已被删除");
        await loadConversations();
      } else {
        toast(err.message, "error");
      }
    }
  }

  async function createConversation({ silent = false } = {}) {
    const data = await api("/api/conversations", {
      method: "POST",
      body: { mode: state.mode, kb: state.kbId },
    });
    state.activeId = data.conversation.id;
    state.messages = [];
    state.conversations.unshift(data.conversation);
    renderConversations();
    updateHeader(data.conversation);
    showEmptyState();
    if (!silent) {
      closeMobileSidebar();
      $("#message-input").focus();
    }
    return data.conversation;
  }

  async function newChat() {
    if (state.sending) {
      toast("回答生成中，请稍候");
      return;
    }
    const current = state.conversations.find((c) => c.id === state.activeId);
    if (current && !current.message_count) {
      closeMobileSidebar();
      $("#message-input").focus();
      return;
    }
    try {
      await createConversation();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function ensureConversation() {
    if (state.activeId) return;
    await createConversation({ silent: true });
  }

  async function renameConversation(conv) {
    const title = await promptDialog("重命名会话", "给这段对话一个更好记的名字", conv.title);
    if (title === null) return;
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(conv.id)}`, {
        method: "PATCH",
        body: { title },
      });
      const index = state.conversations.findIndex((c) => c.id === conv.id);
      if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...data.conversation };
      if (conv.id === state.activeId) updateHeader(data.conversation);
      renderConversations();
      toast("已重命名");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function deleteConversation(conv, { confirm = true } = {}) {
    if (confirm) {
      const ok = await confirmDialog(
        "删除这段对话？",
        `「${conv.title}」及其中的 ${conv.message_count || 0} 条消息会永久删除，无法恢复。`,
        "删除"
      );
      if (!ok) return;
    }
    try {
      await api(`/api/conversations/${encodeURIComponent(conv.id)}`, { method: "DELETE" });
      state.conversations = state.conversations.filter((c) => c.id !== conv.id);
      if (state.activeId === conv.id) {
        state.activeId = null;
        state.messages = [];
        state.citations = [];
        $("#messages").innerHTML = "";
        showEmptyState();
        updateHeader(null);
        if (state.conversations.length) await selectConversation(state.conversations[0].id, { silent: true });
      }
      renderConversations();
      toast("对话已删除");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function clearCurrentConversation() {
    const conv = state.conversations.find((c) => c.id === state.activeId);
    if (!conv) return;
    await deleteConversation(conv);
  }

  /* ------------------------------------------------------------- header */
  function updateHeader(conversation) {
    const title = conversation?.title || "新的对话";
    $("#conversation-title").textContent = title;
    const count = conversation?.message_count || state.messages.length || 0;
    const modeText = state.mode === "agent" ? "Agent 模式" : "Chat 模式";
    const kbName = currentKb()?.name || "";
    $("#conversation-meta").textContent = count
      ? `${modeText} · ${count} 条消息${kbName ? ` · ${kbName}` : ""}`
      : `${modeText} · 新会话${kbName ? ` · ${kbName}` : ""}`;
    document.title = `${title} · 星环 Chat`;
  }

  function setMode(mode, opts = {}) {
    state.mode = mode === "rag" ? "rag" : "agent";
    $$(".mode-switch button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === state.mode);
    });
    $$(".composer-switch button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.composerMode === state.mode);
    });
    updateAgentPicker();
    if (state.mode !== "agent") closeAgentMenu();
    if (state.mode === "agent") state.live = false;
    updateLiveToggle();
    $("#composer-hint").textContent =
      state.mode === "agent"
        ? "可调用导入 Agent 与脚本"
        : state.live
          ? "Chat + 联网连接器"
          : "检索知识库后直接回答";
    if (!opts.silent) {
      const conv = state.conversations.find((c) => c.id === state.activeId);
      if (conv) conv.mode = state.mode;
      updateHeader(conv || null);
    }
  }

  /* ------------------------------------------------------------ messages */
  function showEmptyState() {
    $("#empty-state").hidden = false;
    $("#messages").innerHTML = "";
    state.citations = [];
    renderCitations();
    state.graph = state.globalGraph || null;
    state.graphQuery = false;
    renderGraph(state.graph);
  }

  function resetEmptyState() {
    $("#messages").innerHTML = "";
    $("#empty-state").hidden = false;
    $("#conversation-title").textContent = "新的对话";
    $("#conversation-meta").textContent = "Agent 模式 · 新会话";
    setMode(state.mode, { silent: true });
    state.citations = [];
    renderCitations();
    renderGraph(state.graph);
  }

  function hideEmptyState() {
    $("#empty-state").hidden = true;
  }

  function renderMessages() {
    const wrap = $("#messages");
    wrap.innerHTML = "";
    if (!state.messages.length) {
      showEmptyState();
      state.citations = [];
      renderCitations();
      return;
    }
    hideEmptyState();
    state.messages.forEach((message, index) => {
      wrap.append(buildMessage(message, { isLast: index === state.messages.length - 1 }));
    });
    const lastAssistant = [...state.messages].reverse().find((m) => m.role === "assistant");
    if (lastAssistant) {
      state.citations = lastAssistant.citations || [];
      renderCitations();
      if (lastAssistant.meta?.graph?.nodes?.length) {
        state.graph = lastAssistant.meta.graph;
        state.graphQuery = true;
        renderGraph(state.graph, { reset: true });
      } else if (state.globalGraph) {
        state.graph = state.globalGraph;
        state.graphQuery = false;
        renderGraph(state.graph);
      }
    }
    const scroller = $("#chat-scroll");
    requestAnimationFrame(() => {
      scroller.scrollTop = scroller.scrollHeight;
    });
  }

  function buildMessage(message, { isLast = false, streaming = false } = {}) {
    const article = document.createElement("article");
    article.className = `msg ${message.role}` + (streaming ? " streaming" : "");
    article.dataset.id = message.id || "";

    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    if (message.role === "assistant") {
      avatar.innerHTML = `<svg class="icon"><use href="#i-sparkle"/></svg>`;
    }

    const body = document.createElement("div");
    body.className = "msg-body";

    if (message.role === "assistant") {
      const head = document.createElement("div");
      head.className = "msg-head";
      const author = document.createElement("span");
      author.className = "msg-author";
      author.textContent = "星环";
      const time = document.createElement("span");
      time.className = "msg-time";
      time.textContent = timeLabel(message.created_at);
      head.append(author, time);

      const meta = message.meta || {};
      if (meta.engine === "imported") {
        head.append(badge(`导入 ${meta.pipeline ? "Pipeline" : meta.agent_kind === "skill" ? "Skill" : "Agent"}`, "mint"));
      }
      else if (meta.engine === "external") head.append(badge("外部 Agent", "accent"));
      else if (meta.mode === "agent") head.append(badge("Agent", "accent"));
      else if (meta.mode === "rag") head.append(badge("Chat", ""));
      if (meta.answer_mode === "llm") head.append(badge("LLM 生成", "mint"));
      else if (meta.answer_mode === "llm_general") head.append(badge("LLM 通用", ""));
      else if (meta.answer_mode === "tool_loop") head.append(badge("工具循环", "mint"));
      else if (meta.answer_mode === "extractive") head.append(badge("离线抽取", ""));
      if (meta.requires_model) head.append(badge("需要 LLM", ""));
      if (meta.confidence && Number(meta.confidence) > 0) {
        head.append(badge(`置信度 ${Math.round(Number(meta.confidence) * 100)}%`, ""));
      }
      if (streaming) head.append(badge("正在生成", "accent"));
      body.append(head);
    }

    const content = document.createElement("div");
    content.className = "msg-content";
    if (message.content) content.innerHTML = renderMarkdown(message.content);

    if (message.role === "user") {
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";
      bubble.append(content);
      if (!streaming) {
        // Put time/copy at the end of the last text line, not on a new row.
        const meta = buildUserMeta(message);
        const lastBlock = content.lastElementChild;
        if (lastBlock) lastBlock.append(meta);
        else content.append(meta);
      }
      body.append(bubble);
    } else {
      body.append(content);
    }

    if (streaming) {
      const live = document.createElement("div");
      live.className = "live-status";
      live.innerHTML = '<span class="spinner"></span><span class="live-title">正在准备回答</span><em class="live-detail"></em>';
      body.append(live);
    }

    if (message.role === "assistant" && !streaming) {
      const meta = message.meta || {};
      if (meta.execution?.ran) {
        body.append(buildExecution(meta.execution));
      }
      if (meta.trace?.length) {
        body.append(buildTrace(meta.trace, isLast && meta.mode === "agent"));
      }
      if (meta.error) {
        const err = document.createElement("div");
        err.className = "msg-error";
        err.textContent = `生成时遇到问题：${meta.error}`;
        body.append(err);
      }
      if (meta.warnings?.length) {
        const warning = document.createElement("div");
        warning.className = "msg-warning";
        warning.textContent = meta.warnings.join("；");
        body.append(warning);
      }
      if (meta.suggestions?.length && isLast) {
        const followups = document.createElement("div");
        followups.className = "suggestion-followups";
        for (const suggestion of meta.suggestions) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "followup";
          btn.innerHTML = `<svg class="icon"><use href="#i-sparkle"/></svg><span></span>`;
          btn.querySelector("span").textContent = suggestion;
          btn.addEventListener("click", () => askQuestion(suggestion));
          followups.append(btn);
        }
        body.append(followups);
      }
    }

    if (!streaming && message.role !== "user") {
      body.append(buildTools(message));
    }

    if (message.role === "assistant") article.append(avatar, body);
    else article.append(body);
    return article;
  }

  function badge(text, kind) {
    const span = document.createElement("span");
    span.className = "msg-badge" + (kind ? " " + kind : "");
    span.textContent = text;
    return span;
  }

  async function copyMessage(message) {
    try {
      await navigator.clipboard.writeText(message.content || "");
      toast("已复制到剪贴板");
    } catch {
      toast("浏览器未允许复制", "error");
    }
  }

  function copyButton(message, label = "复制") {
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "tool-btn";
    copy.title = "复制";
    copy.setAttribute("aria-label", "复制");
    copy.innerHTML = `<svg class="icon"><use href="#i-copy"/></svg>`;
    if (label) {
      const text = document.createElement("span");
      text.textContent = label;
      copy.append(text);
    }
    copy.addEventListener("click", () => copyMessage(message));
    return copy;
  }

  function buildUserMeta(message) {
    const meta = document.createElement("span");
    meta.className = "msg-user-meta";
    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = timeLabel(message.created_at);
    meta.append(time, copyButton(message, ""));
    return meta;
  }

  function buildTools(message) {
    const tools = document.createElement("div");
    tools.className = "msg-tools";
    if (message.content) {
      tools.append(copyButton(message));
    }
    if (message.citations?.length) {
      const divider = document.createElement("span");
      divider.className = "tool-divider";
      tools.append(divider);
      const citations = document.createElement("button");
      citations.type = "button";
      citations.className = "tool-btn";
      citations.innerHTML = `<svg class="icon"><use href="#i-list"/></svg><span>${message.citations.length} 条引用</span>`;
      citations.addEventListener("click", () => {
        state.citations = message.citations;
        renderCitations();
        switchPanelView("citations");
        openPanel();
      });
      tools.append(citations);
    }
    if (message.meta?.trace?.length) {
      const divider = document.createElement("span");
      divider.className = "tool-divider";
      tools.append(divider);
      const trace = document.createElement("button");
      trace.type = "button";
      trace.className = "tool-btn";
      trace.innerHTML = `<svg class="icon"><use href="#i-orbit"/></svg><span>${message.meta.trace.length} 步轨迹</span>`;
      trace.addEventListener("click", () => {
        const details = tools.parentElement.querySelector("details.trace");
        if (details) {
          details.open = !details.open;
          details.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      });
      tools.append(trace);
    }
    return tools;
  }

  function buildTrace(trace, open) {
    const details = document.createElement("details");
    details.className = "trace";
    if (open) details.open = true;
    const total = trace.reduce((sum, step) => sum + (Number(step.duration_ms) || 0), 0);
    const summary = document.createElement("summary");
    summary.innerHTML = `
      <svg class="icon"><use href="#i-orbit"/></svg>
      <span>执行轨迹 · ${trace.length} 步${total ? ` · ${total} ms` : ""}</span>
      <svg class="icon chev"><use href="#i-chevron"/></svg>
    `;
    const body = document.createElement("div");
    body.className = "trace-body";
    for (const step of trace) {
      const row = document.createElement("div");
      row.className = "trace-step " + (step.kind || "tool") + (step.status === "error" ? " error" : "");
      const dot = document.createElement("span");
      dot.className = "trace-dot";
      dot.innerHTML =
        step.status === "error"
          ? `<svg class="icon"><use href="#i-alert"/></svg>`
          : `<svg class="icon"><use href="#i-check"/></svg>`;
      const text = document.createElement("div");
      const title = document.createElement("div");
      title.className = "trace-title";
      title.textContent = step.title || "步骤";
      if (step.duration_ms) {
        const time = document.createElement("span");
        time.className = "trace-time";
        time.textContent = `${step.duration_ms} ms`;
        title.append(time);
      }
      const detail = document.createElement("div");
      detail.className = "trace-detail";
      detail.textContent = step.detail || "";
      text.append(title, detail);
      if (step.logic || step.code) {
        const logic = document.createElement("details");
        logic.className = "trace-logic";
        const cap = document.createElement("summary");
        cap.textContent = "代码逻辑";
        logic.append(cap);
        if (step.logic) {
          const p = document.createElement("p");
          p.textContent = step.logic;
          logic.append(p);
        }
        if (step.code) {
          const pre = document.createElement("pre");
          pre.textContent = step.code;
          logic.append(pre);
        }
        text.append(logic);
      }
      row.append(dot, text);
      body.append(row);
    }
    details.append(summary, body);
    return details;
  }

  function buildExecution(exec) {
    const details = document.createElement("details");
    details.className = "execution";
    details.open = true;
    const summary = document.createElement("summary");
    const command = exec.command || "script";
    summary.innerHTML = `
      <svg class="icon"><use href="#i-terminal"/></svg>
      <span>脚本执行 · ${escapeHtml(command)}</span>
      <span class="exec-code">exit ${escapeHtml(String(exec.exit_code ?? "?"))} · ${Number(exec.duration_ms) || 0} ms</span>
    `;
    const body = document.createElement("div");
    body.className = "execution-body";
    const addBlock = (label, text, cls = "") => {
      const title = document.createElement("div");
      title.className = "execution-label";
      title.textContent = label;
      const pre = document.createElement("pre");
      if (cls) pre.className = cls;
      pre.textContent = text || "(无输出)";
      body.append(title, pre);
    };
    if (exec.timed_out) {
      const note = document.createElement("div");
      note.className = "execution-empty";
      note.textContent = "执行超时，已终止进程并回退到知识库回答。";
      body.append(note);
    }
    if (exec.stdout) addBlock("STDOUT", exec.stdout);
    if (exec.stderr) addBlock("STDERR", exec.stderr, "stderr");
    if (!exec.stdout && !exec.stderr) {
      const note = document.createElement("div");
      note.className = "execution-empty";
      note.textContent = "脚本没有产生输出。";
      body.append(note);
    }
    if (exec.run_id && Array.isArray(exec.artifacts) && exec.artifacts.length) {
      const label = document.createElement("div");
      label.className = "execution-label";
      label.textContent = "生成文件";
      body.append(label);
      const list = document.createElement("div");
      list.className = "execution-artifacts";
      for (const artifact of exec.artifacts) {
        const name = artifact.name || "artifact";
        const link = document.createElement("a");
        const path = name.split("/").map(encodeURIComponent).join("/");
        link.href = `/api/agent-runs/${encodeURIComponent(exec.run_id)}/artifacts/${path}`;
        link.className = "execution-artifact";
        link.innerHTML = `<svg class="icon"><use href="#i-book"/></svg><span></span><em></em>`;
        link.querySelector("span").textContent = name;
        link.querySelector("em").textContent = formatBytes(artifact.size);
        link.addEventListener("click", (event) => event.stopPropagation());
        list.append(link);
      }
      body.append(list);
    }
    details.append(summary, body);
    return details;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }

  function setLiveStatus(el, title, detail) {
    const live = el.querySelector(".live-status");
    if (!live) return;
    live.querySelector(".live-title").textContent = title || "正在思考";
    live.querySelector(".live-detail").textContent = detail || "";
  }

  function updateStreamingContent(el, text) {
    const content = el.querySelector(".msg-content");
    content.innerHTML = renderMarkdown(text);
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    content.append(cursor);
  }

  /* ------------------------------------------------------------- sending */
  async function askQuestion(question) {
    const input = $("#message-input");
    input.value = question;
    resizeInput();
    updateComposer();
    await submitMessage();
  }

  async function submitMessage() {
    if (state.sending) return;
    const input = $("#message-input");
    const text = input.value.trim();
    if (!text) return;
    try {
      await ensureConversation();
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (!state.activeId) return;

    state.sending = true;
    input.value = "";
    resizeInput();
    updateComposer();
    hideEmptyState();

    const now = new Date().toISOString();
    const userMessage = {
      id: "local_user",
      role: "user",
      content: text,
      created_at: now,
      citations: [],
      meta: { mode: state.mode, live: state.live },
    };
    state.messages.push(userMessage);
    $("#messages").append(buildMessage(userMessage));

    const assistant = {
      id: "local_stream",
      role: "assistant",
      content: "",
      created_at: now,
      citations: [],
      meta: { mode: state.mode, engine: state.meta?.agent_enabled ? "external" : "local" },
    };
    const streamEl = buildMessage(assistant, { isLast: true, streaming: true });
    $("#messages").append(streamEl);
    const scroller = $("#chat-scroll");
    scroller.scrollTop = scroller.scrollHeight;
    setLiveStatus(streamEl, "准备检索", "正在连接知识库与 Agent");
    updateHeader(state.conversations.find((c) => c.id === state.activeId) || null);

    let finalMessage = null;
    let hadSseError = false;
    try {
      const res = await fetch(
        `/api/conversations/${encodeURIComponent(state.activeId)}/messages`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          credentials: "same-origin",
          body: JSON.stringify({
            content: text,
            mode: state.mode,
            agent_id: state.mode === "agent" ? state.agentId : null,
            live: state.live,
          }),
        }
      );
      if (res.status === 401) {
        showAuth();
        throw new ApiError("登录已过期，请重新登录", 401);
      }
      if (!res.ok || !res.body) {
        const raw = await res.text();
        let message = "请求失败";
        try {
          message = JSON.parse(raw).detail || message;
        } catch {
          /* noop */
        }
        throw new ApiError(message, res.status);
      }

      let buffer = "";
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = parseSseBlock(block);
          if (!event) continue;
          const { name, data } = event;
          if (name === "meta") {
            const userEls = $$("#messages .msg.user");
            const user = userEls[userEls.length - 1];
            if (user && data.user_message) user.dataset.id = data.user_message.id;
            if (data.title) {
              const conv = state.conversations.find((c) => c.id === state.activeId);
              if (conv) conv.title = data.title;
              $("#conversation-title").textContent = data.title;
              renderConversations();
            }
          } else if (name === "status") {
            setLiveStatus(streamEl, data.title, data.detail);
          } else if (name === "delta") {
            assistant.content += data.text || "";
            updateStreamingContent(streamEl, assistant.content);
            const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
            if (near) scroller.scrollTop = scroller.scrollHeight;
          } else if (name === "done") {
            finalMessage = data.message;
            const conv = data.conversation;
            if (conv) {
              const index = state.conversations.findIndex((c) => c.id === conv.id);
              if (index >= 0) state.conversations[index] = conv;
              else state.conversations.unshift(conv);
              state.messages = state.messages.filter((m) => m.id !== "local_stream" && !(m.role === "user" && m.id === "local_user"));
              state.messages.push(
                data.user_message || { ...userMessage, id: userMessage.id },
                finalMessage
              );
              updateHeader(conv);
              renderConversations();
              state.citations = finalMessage.citations || [];
              renderCitations();
              if (finalMessage.meta?.graph?.nodes?.length) {
                state.graph = finalMessage.meta.graph;
                state.graphQuery = true;
                renderGraph(state.graph, { reset: true });
              } else if (state.globalGraph) {
                state.graph = state.globalGraph;
                state.graphQuery = false;
                renderGraph(state.graph);
              }
            }
            const replacement = buildMessage(finalMessage, { isLast: true });
            streamEl.replaceWith(replacement);
            streamEl.dataset.replaced = "1";
            scroller.scrollTop = scroller.scrollHeight;
          } else if (name === "error") {
            hadSseError = true;
            const errorBox = document.createElement("div");
            errorBox.className = "msg-error";
            errorBox.textContent = data.detail || "回答生成失败";
            streamEl.querySelector(".live-status")?.remove();
            streamEl.querySelector(".msg-content").append(errorBox);
          }
        }
      }
      if (!finalMessage && !streamEl.dataset.replaced && !hadSseError) {
        throw new ApiError("连接中断，回答没有完整返回", 0);
      }
    } catch (err) {
      if (err.status === 401) {
        toast("登录已过期，请重新登录", "error");
      } else {
        if (!streamEl.dataset.replaced && !hadSseError) {
          const errorBox = document.createElement("div");
          errorBox.className = "msg-error";
          errorBox.textContent = err.message || "网络异常，回答生成失败";
          streamEl.querySelector(".live-status")?.remove();
          streamEl.querySelector(".msg-content").append(errorBox);
        }
        toast(err.message || "请求失败", "error");
      }
      if (!state.messages.some((m) => m.id === "local_stream")) {
        state.messages.push({ ...assistant, id: "local_stream" });
      }
    } finally {
      state.sending = false;
      updateComposer();
      const conv = state.conversations.find((c) => c.id === state.activeId);
      if (conv) updateHeader(conv);
      if (!finalMessage && !streamEl.dataset.replaced) {
        streamEl.classList.remove("streaming");
        streamEl.querySelector(".live-status")?.remove();
      }
      $("#message-input").focus({ preventScroll: true });
      if (state.conversations.length) loadConversations();
    }
  }

  function parseSseBlock(block) {
    let name = "message";
    const dataLines = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    try {
      return { name, data: JSON.parse(dataLines.join("\n")) };
    } catch {
      return null;
    }
  }

  function resizeInput() {
    const input = $("#message-input");
    input.style.height = "auto";
    input.style.height = Math.min(190, Math.max(26, input.scrollHeight)) + "px";
  }

  function updateLiveToggle() {
    const button = $("#live-toggle");
    if (!button) return;
    button.hidden = state.mode !== "rag";
    button.classList.toggle("active", state.live);
    button.setAttribute("aria-pressed", String(state.live));
    const label = button.querySelector("span");
    if (label) label.textContent = state.live ? "联网 · 开" : "联网";
  }

  function updateComposer() {
    const input = $("#message-input");
    $("#send-btn").disabled = state.sending || !input.value.trim();
    $("#send-btn").classList.toggle("sending", state.sending);
    const cancelBtn = $("#cancel-btn");
    if (cancelBtn) {
      cancelBtn.hidden = !state.sending;
      cancelBtn.disabled = false;
      cancelBtn.textContent = "停止";
    }
    $("#composer-hint").textContent = state.sending
      ? "正在生成回答…"
      : state.mode === "agent"
        ? "可调用导入 Agent 与脚本"
        : state.live
          ? "Chat + 联网连接器"
          : "检索知识库后直接回答";
    updateLiveToggle();
    updateAgentPicker();
  }

  /* ------------------------------------------------------------ citations */
  function renderCitations() {
    const list = $("#citation-list");
    list.innerHTML = "";
    $("#citation-count").textContent = String(state.citations.length);
    if (!state.citations.length) {
      list.innerHTML = `
        <div class="citation-empty">
          <svg class="icon"><use href="#i-quote"/></svg>
          <div>回答之后，这里会出现<br>可回查的原文引用</div>
        </div>`;
      return;
    }
    for (const citation of state.citations) {
      const card = document.createElement("div");
      card.className = "citation-card";
      card.innerHTML = `
        <div class="citation-card-top">
          <span class="citation-index">${escapeHtml(citation.index ?? "")}</span>
          <span class="citation-title"></span>
        </div>
        <p class="citation-snippet"></p>
      `;
      card.querySelector(".citation-title").textContent =
        citation.label || `《${citation.doc_title || "文档"}》`;
      card.querySelector(".citation-snippet").textContent = citation.snippet || "";
      card.addEventListener("click", () => showCitationModal(citation));
      list.append(card);
    }
  }

  function showCitationModal(citation) {
    const root = $("#modal-root");
    root.hidden = false;
    root.innerHTML = "";
    const card = document.createElement("div");
    card.className = "modal-card";
    card.innerHTML = `
      <div class="citation-card-top">
        <span class="citation-index">${escapeHtml(citation.index ?? "")}</span>
        <span class="citation-title"></span>
      </div>
      <p class="citation-snippet" style="margin-top:12px;font-size:12px;line-height:1.85;-webkit-line-clamp:unset;"></p>
      <div class="modal-actions"><button type="button" class="btn ghost">关闭</button></div>
    `;
    card.querySelector(".citation-title").textContent =
      citation.label || `《${citation.doc_title || "文档"}》`;
    const snippetEl = card.querySelector(".citation-snippet");
    snippetEl.textContent = citation.snippet || "";
    if (citation.url) {
      const sourceLine = document.createElement("p");
      sourceLine.className = "citation-source-line";
      sourceLine.textContent = `${citation.domain || ""}${citation.published_at ? ` · ${citation.published_at.slice(0, 10)}` : ""}`;
      snippetEl.after(sourceLine);
    }
    const actions = card.querySelector(".modal-actions");
    if (citation.url && /^https?:\/\//i.test(citation.url)) {
      const link = document.createElement("a");
      link.className = "btn ghost";
      link.href = citation.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "打开原文";
      actions.prepend(link);
    }
    card.querySelector("button").addEventListener("click", () => {
      root.hidden = true;
    });
    root.append(card);
    root.onclick = (event) => {
      if (event.target === root) root.hidden = true;
    };
  }

  /* --------------------------------------------------------------- graph */
  async function loadGlobalGraph() {
    if (!state.kbId) return;
    try {
      const graph = await api(`/api/kb/graph?kb=${encodeURIComponent(state.kbId)}`);
      state.globalGraph = graph;
      state.graph = graph;
      state.graphQuery = false;
      renderGraph(graph, { reset: true });
    } catch (err) {
      toast("图谱加载失败：" + err.message, "error");
    }
  }

  function renderGraph(graph, { reset = false } = {}) {
    const svg = $("#graph-svg");
    const viewport = $("#graph-viewport");
    const empty = $("#graph-empty");
    viewport.innerHTML = "";
    if (reset) state.graphTransform = { x: 0, y: 0, k: 1 };
    if (!graph || !graph.nodes?.length) {
      empty.hidden = false;
      $("#graph-count").textContent = "0";
      $("#graph-legend").innerHTML = "";
      $("#graph-subtitle").textContent = "知识库全局视图";
      return;
    }
    empty.hidden = true;
    $("#graph-subtitle").textContent = state.graphQuery ? "当前问题的 2 跳子图" : "知识库全局视图";

    const W = 600;
    const H = 440;
    const ids = new Set(graph.nodes.map((n) => n.id));
    const nodes = graph.nodes.slice(0, 34).map((node, index) => ({
      ...node,
      x: W / 2 + Math.cos((index / Math.max(1, graph.nodes.length)) * Math.PI * 2) * (90 + (index % 5) * 16),
      y: H / 2 + Math.sin((index / Math.max(1, graph.nodes.length)) * Math.PI * 2) * (70 + (index % 7) * 10),
      vx: 0,
      vy: 0,
    }));
    const indexById = new Map(nodes.map((n, i) => [n.id, i]));
    const edges = (graph.edges || [])
      .filter((e) => ids.has(e.src) && ids.has(e.dst) && indexById.has(e.src) && indexById.has(e.dst))
      .slice(0, 90);

    // Small deterministic force layout: repulsion + springs + centering.
    for (let iteration = 0; iteration < 150; iteration += 1) {
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = nodes[i];
          const b = nodes[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let dist = Math.hypot(dx, dy) || 0.1;
          const force = 2100 / (dist * dist + 80);
          dx = (dx / dist) * force;
          dy = (dy / dist) * force;
          a.vx += dx;
          a.vy += dy;
          b.vx -= dx;
          b.vy -= dy;
        }
      }
      for (const edge of edges) {
        const a = nodes[indexById.get(edge.src)];
        const b = nodes[indexById.get(edge.dst)];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 0.1;
        const target = 88;
        const force = (dist - target) * 0.012;
        dx = (dx / dist) * force;
        dy = (dy / dist) * force;
        a.vx += dx;
        a.vy += dy;
        b.vx -= dx;
        b.vy -= dy;
      }
      for (const node of nodes) {
        node.vx += (W / 2 - node.x) * 0.0035;
        node.vy += (H / 2 - node.y) * 0.0035;
        node.x += Math.max(-8, Math.min(8, node.vx));
        node.y += Math.max(-8, Math.min(8, node.vy));
        node.vx *= 0.72;
        node.vy *= 0.72;
        node.x = Math.max(34, Math.min(W - 34, node.x));
        node.y = Math.max(30, Math.min(H - 30, node.y));
      }
    }

    const degree = {};
    for (const edge of edges) {
      degree[edge.src] = (degree[edge.src] || 0) + 1;
      degree[edge.dst] = (degree[edge.dst] || 0) + 1;
    }
    for (const node of nodes) {
      node.r = Math.min(17, 7 + Math.sqrt(degree[node.id] || 1) * 2 + Math.min(4, (node.mentions || 0) * 0.16));
    }
    const nodeById = new Map(nodes.map((n) => [n.id, n]));
    const ns = "http://www.w3.org/2000/svg";

    const edgeLayer = document.createElementNS(ns, "g");
    const labelLayer = document.createElementNS(ns, "g");
    const nodeLayer = document.createElementNS(ns, "g");
    viewport.append(edgeLayer, labelLayer, nodeLayer);

    const sortedEdges = [...edges].sort((a, b) => (Number(b.weight) || 0) - (Number(a.weight) || 0));
    sortedEdges.forEach((edge, index) => {
      const a = nodeById.get(edge.src);
      const b = nodeById.get(edge.dst);
      if (!a || !b) return;
      const line = document.createElementNS(ns, "line");
      line.setAttribute("x1", a.x);
      line.setAttribute("y1", a.y);
      line.setAttribute("x2", b.x);
      line.setAttribute("y2", b.y);
      line.setAttribute("class", "graph-edge");
      line.dataset.src = edge.src;
      line.dataset.dst = edge.dst;
      const title = document.createElementNS(ns, "title");
      title.textContent = `${edge.src_name} —${edge.type}→ ${edge.dst_name}`;
      line.append(title);
      edgeLayer.append(line);
      if (index < 7 && Math.hypot(b.x - a.x, b.y - a.y) > 62) {
        const label = document.createElementNS(ns, "text");
        label.setAttribute("x", (a.x + b.x) / 2);
        label.setAttribute("y", (a.y + b.y) / 2 - 3);
        label.setAttribute("class", "graph-edge-label");
        label.textContent = edge.type;
        labelLayer.append(label);
      }
    });

    nodes.forEach((node) => {
      const group = document.createElementNS(ns, "g");
      group.setAttribute("class", "graph-node");
      group.setAttribute("transform", `translate(${node.x} ${node.y})`);
      group.dataset.id = node.id;
      const color = TYPE_COLORS[node.type] || "#9aa7bd";
      const halo = document.createElementNS(ns, "circle");
      halo.setAttribute("class", "node-halo");
      halo.setAttribute("r", node.r + 10);
      halo.setAttribute("fill", color);
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("class", "node-dot");
      dot.setAttribute("r", node.r);
      dot.setAttribute("fill", color);
      const label = document.createElementNS(ns, "text");
      label.setAttribute("y", node.r + 14);
      label.textContent = truncate(node.name, 9);
      const title = document.createElementNS(ns, "title");
      title.textContent = `${node.name} · ${node.type}${node.mentions ? ` · 提及 ${node.mentions} 次` : ""}`;
      group.append(halo, dot, label, title);
      group.addEventListener("click", (event) => {
        event.stopPropagation();
        openEntity(node.id);
      });
      group.addEventListener("mouseenter", () => highlightNode(node.id, true));
      group.addEventListener("mouseleave", () => highlightNode(node.id, false));
      nodeLayer.append(group);
    });

    applyGraphTransform();
    $("#graph-count").textContent = `${nodes.length} 节点 · ${edges.length} 关系`;
    const typeCounts = {};
    for (const node of nodes) typeCounts[node.type] = (typeCounts[node.type] || 0) + 1;
    const legend = $("#graph-legend");
    legend.innerHTML = "";
    Object.entries(typeCounts)
      .sort((a, b) => b[1] - a[1])
      .forEach(([type, count]) => {
        const item = document.createElement("span");
        item.className = "legend-item";
        item.innerHTML = `<span class="legend-swatch" style="background:${TYPE_COLORS[type] || "#9aa7bd"}"></span>${escapeHtml(type)} ${count}`;
        legend.append(item);
      });
    state._graphNodes = nodes;
    state._graphEdges = edges;
  }

  function highlightNode(id, on) {
    const viewport = $("#graph-viewport");
    const nodes = state._graphNodes || [];
    const connected = new Set([id]);
    for (const edge of state._graphEdges || []) {
      if (edge.src === id) connected.add(edge.dst);
      if (edge.dst === id) connected.add(edge.src);
    }
    $$(".graph-node", viewport).forEach((node) => {
      const nodeId = node.dataset.id;
      node.classList.toggle("dim", on && !connected.has(nodeId));
    });
    $$(".graph-edge", viewport).forEach((edge) => {
      const hot = on && (edge.dataset.src === id || edge.dataset.dst === id);
      edge.classList.toggle("hot", hot);
    });
  }

  function applyGraphTransform() {
    const viewport = $("#graph-viewport");
    const { x, y, k } = state.graphTransform;
    viewport.setAttribute("transform", `translate(${x} ${y}) scale(${k})`);
  }

  function zoomGraph(factor) {
    const next = Math.max(0.5, Math.min(2.6, state.graphTransform.k * factor));
    state.graphTransform.k = next;
    applyGraphTransform();
  }

  function resetGraph() {
    state.graphTransform = { x: 0, y: 0, k: 1 };
    applyGraphTransform();
  }

  function bindGraphInteractions() {
    const svg = $("#graph-svg");
    let dragging = false;
    let start = null;
    svg.addEventListener("pointerdown", (event) => {
      if (event.target.closest(".graph-node")) return;
      dragging = true;
      start = {
        x: event.clientX,
        y: event.clientY,
        tx: state.graphTransform.x,
        ty: state.graphTransform.y,
      };
      svg.classList.add("dragging");
      svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!dragging || !start) return;
      const scale = 600 / svg.getBoundingClientRect().width;
      state.graphTransform.x = start.tx + (event.clientX - start.x) * scale;
      state.graphTransform.y = start.ty + (event.clientY - start.y) * scale;
      applyGraphTransform();
    });
    const end = () => {
      dragging = false;
      start = null;
      svg.classList.remove("dragging");
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        zoomGraph(event.deltaY < 0 ? 1.1 : 0.9);
      },
      { passive: false }
    );
    $$("[data-graph-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.dataset.graphAction;
        if (action === "in") zoomGraph(1.18);
        else if (action === "out") zoomGraph(0.85);
        else if (action === "reset") resetGraph();
        else if (action === "global") {
          state.graphQuery = false;
          await loadGlobalGraph();
        }
      });
    });
  }

  async function openEntity(id) {
    const sheet = $("#entity-sheet");
    sheet.hidden = false;
    sheet.innerHTML = `<div class="entity-card"><div class="entity-body" style="padding:28px;color:var(--faint);font-size:11px;">正在加载实体…</div></div>`;
    try {
      const entity = await api(
        `/api/kb/entities/${encodeURIComponent(id)}?kb=${encodeURIComponent(state.kbId)}`
      );
      sheet.innerHTML = "";
      const card = document.createElement("div");
      card.className = "entity-card";
      const head = document.createElement("div");
      head.className = "entity-head";
      const type = document.createElement("span");
      type.className = "entity-type";
      type.textContent = (entity.type || "概").slice(0, 1);
      const headText = document.createElement("div");
      headText.className = "entity-head-text";
      const h3 = document.createElement("h3");
      h3.textContent = entity.name;
      const p = document.createElement("p");
      p.textContent = `${entity.type} · 提及 ${entity.mentions || 0} 次`;
      headText.append(h3, p);
      const close = document.createElement("button");
      close.className = "icon-btn";
      close.innerHTML = `<svg class="icon"><use href="#i-x"/></svg>`;
      close.addEventListener("click", () => (sheet.hidden = true));
      head.append(type, headText, close);

      const body = document.createElement("div");
      body.className = "entity-body";
      if (entity.description) {
        const desc = document.createElement("p");
        desc.className = "entity-desc";
        desc.textContent = entity.description;
        body.append(desc);
      }
      if (entity.relations?.length) {
        const section = document.createElement("div");
        section.className = "entity-section";
        section.innerHTML = `<h4>关系 · ${entity.relations.length}</h4>`;
        for (const relation of entity.relations.slice(0, 18)) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "relation-pill";
          btn.innerHTML = `
            <span class="relation-label"></span>
            <span class="relation-other"></span>
            <span class="relation-dir">${relation.direction === "out" ? "→" : "←"}</span>
          `;
          btn.querySelector(".relation-label").textContent = relation.type;
          btn.querySelector(".relation-other").textContent = relation.other;
          btn.title = relation.evidence || "";
          btn.addEventListener("click", () => openEntity(relation.other_id));
          section.append(btn);
        }
        body.append(section);
      }
      if (entity.chunks?.length) {
        const section = document.createElement("div");
        section.className = "entity-section";
        section.innerHTML = `<h4>原文证据</h4>`;
        for (const chunk of entity.chunks.slice(0, 4)) {
          const div = document.createElement("div");
          div.className = "entity-chunk";
          const label = document.createElement("strong");
          label.textContent = `《${chunk.doc_title}》${chunk.heading_path ? " › " + chunk.heading_path : ""}`;
          const text = document.createElement("span");
          text.textContent = chunk.text;
          div.append(label, text);
          section.append(div);
        }
        body.append(section);
      }
      card.append(head, body);
      sheet.append(card);
      sheet.onclick = (event) => {
        if (event.target === sheet) sheet.hidden = true;
      };
    } catch (err) {
      sheet.innerHTML = `<div class="entity-card"><div class="entity-body" style="padding:28px;color:var(--rose);font-size:11px;">${escapeHtml(err.message)}</div></div>`;
    }
  }

  /* --------------------------------------------------------------- panel */
  function switchPanelView(view) {
    state.panelView = view;
    $$(".panel-tab").forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $$(".panel-view").forEach((section) => {
      section.hidden = section.id !== `view-${view}`;
    });
    if (view === "citations") renderCitations();
    if (view === "docs") renderDocs();
    if (view === "graph") applyGraphTransform();
  }

  function isMobilePanel() {
    return window.matchMedia("(max-width: 1120px)").matches;
  }

  function openPanel() {
    const panel = $("#insight-panel");
    if (isMobilePanel()) {
      panel.classList.add("open");
      $("#panel-backdrop").hidden = false;
    } else {
      $("#app-shell").classList.remove("panel-hidden");
    }
  }

  function closePanel() {
    const panel = $("#insight-panel");
    panel.classList.remove("open");
    $("#panel-backdrop").hidden = true;
    if (!isMobilePanel()) $("#app-shell").classList.add("panel-hidden");
  }

  function togglePanel() {
    const panel = $("#insight-panel");
    if (isMobilePanel()) {
      if (panel.classList.contains("open")) closePanel();
      else openPanel();
    } else {
      $("#app-shell").classList.toggle("panel-hidden");
    }
  }

  function syncPanelMode() {
    if (isMobilePanel()) {
      $("#app-shell").classList.remove("panel-hidden");
      $("#insight-panel").classList.remove("open");
    } else {
      $("#app-shell").classList.remove("panel-hidden");
      $("#insight-panel").classList.remove("open");
    }
  }

  function openMobileSidebar() {
    $("#sidebar").classList.add("open");
    $("#sidebar-backdrop").hidden = false;
  }

  function closeMobileSidebar() {
    $("#sidebar").classList.remove("open");
    $("#sidebar-backdrop").hidden = true;
  }

  /* -------------------------------------------------------------- modals */
  function modalShell() {
    const root = $("#modal-root");
    root.hidden = false;
    root.innerHTML = "";
    return root;
  }

  async function openKbDocs() {
    const root = modalShell();
    const card = document.createElement("div");
    card.className = "modal-card kb-docs-modal";
    card.innerHTML = `
      <div class="kb-docs-head">
        <div>
          <p class="eyebrow">KNOWLEDGE DOCUMENTS</p>
          <h3>知识库文档</h3>
          <p>上传后立即解析、分块并建立索引；删除会同时移除源文件。</p>
        </div>
        <button class="icon-btn kb-docs-close" type="button" aria-label="关闭知识库文档">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="kb-docs-toolbar">
        <select data-kb-select aria-label="知识库"></select>
        <label class="kb-upload-label" data-kb-upload hidden>
          上传文档
          <input type="file" accept=".md,.markdown,.txt,.text,.rst,.pdf,.docx,.pptx,.xlsx,.xlsm,.xls" data-kb-file />
        </label>
        <button class="kb-reindex" type="button" data-kb-reindex hidden>重建索引</button>
      </div>
      <div class="kb-docs-body">
        <div class="kb-doc-list"></div>
        <div class="kb-docs-status"></div>
      </div>`;
    root.append(card);
    const close = () => { root.hidden = true; };
    card.querySelector(".kb-docs-close").addEventListener("click", close);
    root.onclick = (event) => { if (event.target === root) close(); };
    const select = card.querySelector("[data-kb-select]");
    const list = card.querySelector(".kb-doc-list");
    const status = card.querySelector(".kb-docs-status");
    const uploadWrap = card.querySelector("[data-kb-upload]");
    const fileInput = card.querySelector("[data-kb-file]");
    const reindexBtn = card.querySelector("[data-kb-reindex]");
    let kbId = state.kbId || state.defaultKb;
    let canManage = false;

    for (const kb of state.kbs || []) {
      const option = document.createElement("option");
      option.value = kb.id;
      option.textContent = kb.name;
      if (kb.id === kbId) option.selected = true;
      select.append(option);
    }
    if (!select.options.length) {
      list.innerHTML = `<div class="kb-docs-empty">当前账号没有可访问的知识库</div>`;
      return;
    }
    if (!kbId) kbId = select.options[0].value;

    function setStatus(text) {
      status.textContent = text || "";
    }

    function render(data) {
      canManage = Boolean(data.can_manage);
      uploadWrap.hidden = !canManage;
      reindexBtn.hidden = !canManage;
      list.innerHTML = "";
      if (!data.documents.length) {
        list.innerHTML = `<div class="kb-docs-empty">这个知识库还没有文档。上传一份 Markdown 开始。</div>`;
        return;
      }
      for (const doc of data.documents) {
        const row = document.createElement("div");
        row.className = "kb-doc-row";
        row.innerHTML = `
          <div class="kb-doc-title">
            <strong>${escapeHtml(doc.title)}</strong>
            <small>${escapeHtml(doc.source_name)}</small>
          </div>
          <div class="kb-doc-meta">${escapeHtml(doc.doc_type || "note")}</div>
          <div class="kb-doc-meta kb-doc-chunks">${formatNumber(doc.chunks)} 块</div>
          <div class="kb-doc-meta">${escapeHtml((doc.updated_at || "").slice(0, 10))}</div>
        `;
        if (canManage) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "kb-doc-delete";
          button.textContent = "删除";
          button.addEventListener("click", async () => {
            if (!confirm(`删除《${doc.title}》及其源文件？`)) return;
            button.disabled = true;
            try {
              await api(`/api/kb/documents/${encodeURIComponent(doc.id)}?kb=${encodeURIComponent(kbId)}`, { method: "DELETE" });
              toast("文档已删除");
              await refresh();
            } catch (err) {
              toast(err.message, "error");
              button.disabled = false;
            }
          });
          row.append(button);
        } else {
          const span = document.createElement("span");
          span.className = "kb-doc-meta";
          span.textContent = "只读";
          row.append(span);
        }
        list.append(row);
      }
    }

    async function refresh() {
      setStatus("加载中…");
      const data = await api(`/api/kb/documents?kb=${encodeURIComponent(kbId)}`);
      render(data);
      setStatus(`共 ${data.documents.length} 篇文档 · ${data.can_manage ? "可管理" : "只读"}`);
    }

    select.addEventListener("change", async () => {
      kbId = select.value;
      try {
        await refresh();
      } catch (err) {
        setStatus(err.message);
      }
    });

    fileInput.addEventListener("change", async () => {
      const file = fileInput.files?.[0];
      if (!file) return;
      const form = new FormData();
      form.append("file", file, file.name);
      setStatus(`正在上传并解析 ${file.name}…`);
      try {
        const res = await fetch(`/api/kb/documents?kb=${encodeURIComponent(kbId)}`, {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "上传失败");
        toast(`已添加 ${data.document?.title || file.name}`);
        await refresh();
        if (kbId === state.kbId) {
          await loadKbCatalog();
          await loadOverview();
        }
      } catch (err) {
        setStatus(err.message);
        toast(err.message, "error");
      } finally {
        fileInput.value = "";
      }
    });

    reindexBtn.addEventListener("click", async () => {
      if (!confirm("重新解析当前知识库全部文档？可能耗时较久。")) return;
      reindexBtn.disabled = true;
      setStatus("正在重建索引…");
      try {
        await api(`/api/kb/reindex?kb=${encodeURIComponent(kbId)}`, { method: "POST" });
        toast("索引已重建");
        await refresh();
        if (kbId === state.kbId) await loadOverview();
      } catch (err) {
        setStatus(err.message);
        toast(err.message, "error");
      } finally {
        reindexBtn.disabled = false;
      }
    });

    try {
      await refresh();
    } catch (err) {
      setStatus(err.message);
    }
  }

  async function openAdminUsers() {
    const root = modalShell();
    const card = document.createElement("div");
    card.className = "modal-card admin-modal";
    card.innerHTML = `
      <div class="admin-head">
        <div>
          <p class="eyebrow">ACCESS CONTROL</p>
          <h3>用户权限</h3>
          <p>部门决定默认知识库和部门 Agent；角色决定管理范围。</p>
        </div>
        <button class="icon-btn admin-close" type="button" aria-label="关闭用户权限">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="admin-body">
        <div class="admin-list"></div>
        <p class="admin-note">admin 可见全部部门；manager/member 默认只看本部门。修改后用户下次请求生效。</p>
      </div>`;
    root.append(card);
    const close = () => { root.hidden = true; };
    card.querySelector(".admin-close").addEventListener("click", close);
    root.onclick = (event) => { if (event.target === root) close(); };
    const list = card.querySelector(".admin-list");
    const departments = state.meta?.departments || ["产品部", "研发部", "人事部"];

    function render(users) {
      list.innerHTML = users.map((user) => `
        <div class="admin-user" data-admin-user="${escapeHtml(user.id)}">
          <div class="admin-user-id">
            <strong>${escapeHtml(user.display_name || user.username)}</strong>
            <small>@${escapeHtml(user.username)}</small>
          </div>
          <label class="admin-field">部门
            <select data-admin-department>
              ${departments.map((department) => `<option value="${escapeHtml(department)}" ${department === user.department ? "selected" : ""}>${escapeHtml(department)}</option>`).join("")}
            </select>
          </label>
          <label class="admin-field">角色
            <select data-admin-role>
              <option value="member" ${user.role === "member" ? "selected" : ""}>member</option>
              <option value="manager" ${user.role === "manager" ? "selected" : ""}>manager</option>
              <option value="admin" ${user.role === "admin" ? "selected" : ""}>admin</option>
            </select>
          </label>
          <button class="admin-save" type="button" data-admin-save>保存</button>
        </div>`).join("");
    }

    async function refresh() {
      try {
        const data = await api("/api/admin/users");
        render(data.users || []);
      } catch (err) {
        list.innerHTML = `<div class="connector-empty">${escapeHtml(err.message)}</div>`;
      }
    }

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-admin-save]");
      if (!button) return;
      const row = button.closest("[data-admin-user]");
      const id = row.dataset.adminUser;
      button.disabled = true;
      try {
        const data = await api(`/api/admin/users/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: {
            department: row.querySelector("[data-admin-department]").value,
            role: row.querySelector("[data-admin-role]").value,
          },
        });
        toast(`已更新 ${data.user.display_name || data.user.username}`);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    await refresh();
  }

  function connectorDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }

  async function openConnectorManager() {
    const root = modalShell();
    const card = document.createElement("div");
    card.className = "modal-card connector-modal";
    card.innerHTML = `
      <div class="connector-head">
        <div>
          <p class="eyebrow">CONNECTORS</p>
          <h3>联网连接器</h3>
          <p>先查目录，再生成声明式 spec；测试、审批后才会进入 Tool Loop。</p>
        </div>
        <button class="icon-btn connector-close" type="button" aria-label="关闭连接器">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="connector-tabs">
        <button class="connector-tab active" data-connector-tab="catalog" type="button">目录</button>
        <button class="connector-tab" data-connector-tab="mine" type="button">我的连接器</button>
        <button class="connector-tab" data-connector-tab="propose" type="button">AI 生成</button>
      </div>
      <div class="connector-body"></div>
      <p class="connector-hint" style="padding:0 20px 16px">只允许 HTTPS、固定 host allowlist、GET/POST；审批前不会注册工具。</p>
    `;
    root.append(card);
    const close = () => { root.hidden = true; };
    card.querySelector(".connector-close").addEventListener("click", close);
    root.onclick = (event) => { if (event.target === root) close(); };

    const body = card.querySelector(".connector-body");
    let catalog = [];
    let connectors = [];
    let activeTab = "catalog";
    let proposed = null;

    function setTab(tab) {
      activeTab = tab;
      card.querySelectorAll("[data-connector-tab]").forEach((item) => {
        item.classList.toggle("active", item.dataset.connectorTab === tab);
      });
      render();
    }

    function setResult(item, payload, isError = false) {
      const box = item.querySelector(".connector-result");
      if (!box) return;
      box.hidden = false;
      box.textContent = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
      box.style.color = isError ? "var(--rose)" : "var(--soft)";
    }

    function renderCatalog() {
      if (!catalog.length) {
        body.innerHTML = `<div class="connector-empty">目录为空。</div>`;
        return;
      }
      body.innerHTML = `<div class="connector-grid">${catalog.map((spec, index) => `
        <article class="connector-card">
          <div class="connector-item-top">
            <strong>${escapeHtml(spec.name)}</strong>
            <span class="connector-badge">KNOWN</span>
          </div>
          <p>${escapeHtml(spec.description || "")}</p>
          <div class="connector-url">${escapeHtml(spec.base_url || "")}</div>
          <div class="connector-actions">
            <button class="primary" data-connector-add="${index}" type="button">添加草稿</button>
          </div>
        </article>`).join("")}</div>`;
    }

    function renderMine() {
      if (!connectors.length) {
        body.innerHTML = `<div class="connector-empty">还没有连接器。<br>从「目录」添加天气连接器，或在「AI 生成」里描述一个 API。</div>`;
        return;
      }
      body.innerHTML = `<div class="connector-list">${connectors.map((connector) => {
        const spec = connector.spec || {};
        const params = Object.entries(spec.params || {});
        const status = connector.status || "draft";
        return `
          <article class="connector-item" data-connector-id="${escapeHtml(connector.id)}">
            <div class="connector-item-top">
              <strong>${escapeHtml(spec.name || connector.name)}</strong>
              <span class="connector-badge ${escapeHtml(status)}">${status === "approved" ? "已注册" : status === "rejected" ? "已拒绝" : "草稿"}</span>
              <time>${connectorDate(connector.updated_at)}</time>
            </div>
            <p>${escapeHtml(spec.description || connector.description || "")}</p>
            <div class="connector-url">${escapeHtml((spec.base_url || "") + (spec.path || ""))}</div>
            ${status === "approved" ? `<div class="connector-url">tool: ${escapeHtml("connector." + (spec.name || connector.name))}</div>` : ""}
            <div class="connector-test">
              ${params.map(([name, config]) => `<input data-connector-arg="${escapeHtml(name)}" type="${config.type === "number" ? "number" : "text"}" placeholder="${escapeHtml(config.description || name)}">`).join("")}
              <button data-connector-test type="button">测试</button>
            </div>
            <div class="connector-actions">
              ${status !== "approved" ? `<button class="primary" data-connector-approve type="button">通过并注册</button>` : ""}
              ${status !== "rejected" ? `<button class="danger" data-connector-reject type="button">拒绝</button>` : ""}
            </div>
            <pre class="connector-result" hidden></pre>
          </article>`;
      }).join("")}</div>`;
    }

    function renderPropose() {
      body.innerHTML = `
        <div class="connector-propose">
          <textarea id="connector-query" maxlength="800" placeholder="描述你需要的只读 API，例如：查询北京天气；或提供一个 HTTPS OpenAPI 端点"></textarea>
          <div class="connector-propose-actions">
            <button class="btn primary" id="connector-propose-btn" type="button">查找 / 生成</button>
          </div>
          <div id="connector-propose-output"></div>
        </div>`;
      const output = body.querySelector("#connector-propose-output");
      if (proposed) {
        if (proposed.source === "known") {
          output.innerHTML = `<div class="connector-grid" style="margin-top:10px">${(proposed.specs || []).map((spec, index) => `
            <article class="connector-card">
              <strong>${escapeHtml(spec.name)}</strong>
              <p>${escapeHtml(spec.description || "")}</p>
              <div class="connector-actions"><button class="primary" data-connector-add="${index}" data-proposed="1" type="button">添加草稿</button></div>
            </article>`).join("")}</div>`;
        } else {
          const editor = document.createElement("textarea");
          editor.className = "connector-spec-editor";
          editor.value = JSON.stringify(proposed.spec, null, 2);
          output.append(editor);
          const save = document.createElement("button");
          save.className = "btn primary";
          save.type = "button";
          save.textContent = "保存草稿";
          save.style.marginTop = "7px";
          save.addEventListener("click", async () => {
            try {
              const spec = JSON.parse(editor.value);
              await addSpec(spec);
            } catch (err) {
              toast(err.message || "JSON 不合法", "error");
            }
          });
          output.append(save);
        }
      }
      const button = body.querySelector("#connector-propose-btn");
      button.addEventListener("click", async () => {
        const query = body.querySelector("#connector-query").value.trim();
        if (!query) return;
        button.disabled = true;
        try {
          proposed = await api("/api/connectors/propose", { method: "POST", body: { query } });
          renderPropose();
          body.querySelector("#connector-query").value = query;
        } catch (err) {
          toast(err.message, "error");
        } finally {
          button.disabled = false;
        }
      });
    }

    function render() {
      if (activeTab === "catalog") renderCatalog();
      else if (activeTab === "mine") renderMine();
      else renderPropose();
    }

    async function refresh() {
      try {
        const [catalogData, mineData] = await Promise.all([
          api("/api/connectors/catalog"),
          api("/api/connectors"),
        ]);
        catalog = catalogData.connectors || [];
        connectors = mineData.connectors || [];
        render();
      } catch (err) {
        body.innerHTML = `<div class="connector-empty">${escapeHtml(err.message)}</div>`;
      }
    }

    async function addSpec(spec) {
      try {
        await api("/api/connectors", { method: "POST", body: { spec } });
        toast("已添加连接器草稿");
        activeTab = "mine";
        card.querySelectorAll("[data-connector-tab]").forEach((item) => {
          item.classList.toggle("active", item.dataset.connectorTab === "mine");
        });
        await refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    }

    card.addEventListener("click", async (event) => {
      const add = event.target.closest("[data-connector-add]");
      if (add) {
        const index = Number(add.dataset.connectorAdd);
        const spec = add.dataset.proposed && proposed ? (proposed.specs || [])[index] : catalog[index];
        if (spec) await addSpec(spec);
        return;
      }
      const item = event.target.closest("[data-connector-id]");
      if (!item) return;
      const id = item.dataset.connectorId;
      const testButton = event.target.closest("[data-connector-test]");
      if (testButton) {
        const arguments_ = {};
        item.querySelectorAll("[data-connector-arg]").forEach((input) => {
          if (input.value !== "") arguments_[input.dataset.connectorArg] = input.value;
        });
        testButton.disabled = true;
        testButton.textContent = "测试中…";
        try {
          const data = await api(`/api/connectors/${encodeURIComponent(id)}/test`, {
            method: "POST",
            body: { arguments: arguments_ },
          });
          setResult(item, data.result);
          toast("测试成功");
        } catch (err) {
          setResult(item, err.message, true);
        } finally {
          testButton.disabled = false;
          testButton.textContent = "测试";
        }
        return;
      }
      if (event.target.closest("[data-connector-approve]")) {
        try {
          const data = await api(`/api/connectors/${encodeURIComponent(id)}/approve`, { method: "POST" });
          toast(`已注册 ${data.tool}`);
          await refresh();
        } catch (err) {
          toast(err.message, "error");
        }
        return;
      }
      if (event.target.closest("[data-connector-reject]")) {
        if (!window.confirm("拒绝这个连接器？它不会注册为工具。")) return;
        try {
          await api(`/api/connectors/${encodeURIComponent(id)}/reject`, { method: "POST" });
          toast("已拒绝");
          await refresh();
        } catch (err) {
          toast(err.message, "error");
        }
      }
    });

    card.querySelectorAll("[data-connector-tab]").forEach((tab) => {
      tab.addEventListener("click", () => setTab(tab.dataset.connectorTab));
    });
    await refresh();
  }

  function memoryKindLabel(kind) {
    return { fact: "事实", preference: "偏好", task: "任务" }[kind] || kind;
  }

  function memoryDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return `${date.getMonth() + 1}月${date.getDate()}日`;
  }

  async function openMemoryManager() {
    const root = modalShell();
    const card = document.createElement("div");
    card.className = "modal-card memory-modal";
    card.innerHTML = `
      <div class="memory-head">
        <div>
          <p class="eyebrow">YOUR MEMORY</p>
          <h3>我的长期记忆</h3>
          <p>只对当前账号可见；进入回答上下文，但不会作为文档引用。</p>
        </div>
        <button class="icon-btn memory-close" type="button" aria-label="关闭记忆">
          <svg class="icon"><use href="#i-x"/></svg>
        </button>
      </div>
      <div class="memory-body">
        <form class="memory-form">
          <select aria-label="记忆类型">
            <option value="fact">事实</option>
            <option value="preference">偏好</option>
            <option value="task">任务</option>
          </select>
          <input maxlength="300" placeholder="例如：回答尽量简洁" aria-label="记忆内容" required>
          <button class="btn primary" type="submit">记住</button>
        </form>
        <div class="memory-filters">
          <button class="memory-filter active" data-memory-filter="all" type="button">全部</button>
          <button class="memory-filter" data-memory-filter="fact" type="button">事实</button>
          <button class="memory-filter" data-memory-filter="preference" type="button">偏好</button>
          <button class="memory-filter" data-memory-filter="task" type="button">任务</button>
          <span class="memory-count"></span>
        </div>
        <div class="memory-list"></div>
        <p class="memory-hint">自动提取只识别明确表达：记住… / 我叫… / 我偏好… / 以后叫我…。删除是不可恢复的硬删除。</p>
      </div>
    `;
    root.append(card);
    const close = () => { root.hidden = true; };
    card.querySelector(".memory-close").addEventListener("click", close);
    root.onclick = (event) => { if (event.target === root) close(); };

    const listEl = card.querySelector(".memory-list");
    const countEl = card.querySelector(".memory-count");
    const form = card.querySelector(".memory-form");
    const input = form.querySelector("input");
    const kind = form.querySelector("select");
    let memories = [];
    let filter = "all";

    function render() {
      const visible = filter === "all" ? memories : memories.filter((m) => m.kind === filter);
      countEl.textContent = `${visible.length} / ${memories.length}`;
      if (!visible.length) {
        listEl.innerHTML = `<div class="memory-empty">还没有这类记忆。<br>可以直接在上方添加，或在对话里说“请记住……”</div>`;
        return;
      }
      listEl.innerHTML = visible.map((memory) => {
        const kindClass = escapeHtml(memory.kind || "fact");
        const source = memory.source_conversation_title
          ? `来自会话《${escapeHtml(memory.source_conversation_title)}》`
          : "来自对话";
        return `
          <article class="memory-card" data-memory-id="${escapeHtml(memory.id)}">
            <div class="memory-card-top">
              <span class="memory-kind ${kindClass}">${memoryKindLabel(memory.kind)}</span>
              <time>${memoryDate(memory.created_at)}</time>
              <button class="memory-delete" type="button" data-memory-delete="${escapeHtml(memory.id)}" title="删除这条记忆">
                <svg class="icon"><use href="#i-trash"/></svg>
              </button>
            </div>
            <p class="memory-content">${escapeHtml(memory.content)}</p>
            <div class="memory-source">${source}${memory.hits ? ` · 已使用 ${memory.hits} 次` : ""}</div>
          </article>`;
      }).join("");
    }

    async function refresh() {
      try {
        const data = await api("/api/memories");
        memories = data.memories || [];
        render();
      } catch (err) {
        listEl.innerHTML = `<div class="memory-empty">${escapeHtml(err.message)}</div>`;
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const content = input.value.trim();
      if (!content) return;
      const button = form.querySelector("button");
      button.disabled = true;
      try {
        const data = await api("/api/memories", {
          method: "POST",
          body: {
            kind: kind.value,
            content,
            importance: kind.value === "preference" ? 0.85 : 0.7,
          },
        });
        input.value = "";
        memories = data.memories || [];
        render();
        toast(data.memory?.status === "duplicate" ? "这条记忆已经存在" : "已记住");
      } catch (err) {
        toast(err.message, "error");
      } finally {
        button.disabled = false;
      }
    });

    card.querySelectorAll("[data-memory-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        filter = button.dataset.memoryFilter;
        card.querySelectorAll("[data-memory-filter]").forEach((item) => {
          item.classList.toggle("active", item === button);
        });
        render();
      });
    });

    listEl.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-memory-delete]");
      if (!button) return;
      const id = button.dataset.memoryDelete;
      if (!window.confirm("删除这条长期记忆？删除后无法恢复。")) return;
      try {
        await api(`/api/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
        memories = memories.filter((memory) => memory.id !== id);
        render();
        toast("已删除");
      } catch (err) {
        toast(err.message, "error");
      }
    });

    await refresh();
  }

  function confirmDialog(title, message, confirmLabel = "确认") {
    return new Promise((resolve) => {
      const root = modalShell();
      const card = document.createElement("div");
      card.className = "modal-card";
      const h3 = document.createElement("h3");
      h3.textContent = title;
      const p = document.createElement("p");
      p.textContent = message;
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn ghost";
      cancel.textContent = "取消";
      const ok = document.createElement("button");
      ok.type = "button";
      ok.className = "btn danger";
      ok.textContent = confirmLabel;
      actions.append(cancel, ok);
      card.append(h3, p, actions);
      root.append(card);
      const finish = (value) => {
        root.hidden = true;
        resolve(value);
      };
      cancel.addEventListener("click", () => finish(false));
      ok.addEventListener("click", () => finish(true));
      root.onclick = (event) => {
        if (event.target === root) finish(false);
      };
      ok.focus();
    });
  }

  function promptDialog(title, hint, value = "") {
    return new Promise((resolve) => {
      const root = modalShell();
      const card = document.createElement("div");
      card.className = "modal-card";
      const h3 = document.createElement("h3");
      h3.textContent = title;
      const p = document.createElement("p");
      p.textContent = hint;
      const input = document.createElement("input");
      input.className = "modal-input";
      input.value = value;
      input.maxLength = 40;
      const actions = document.createElement("div");
      actions.className = "modal-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn ghost";
      cancel.textContent = "取消";
      const ok = document.createElement("button");
      ok.type = "button";
      ok.className = "btn primary";
      ok.style.height = "36px";
      ok.style.fontSize = "11.5px";
      ok.textContent = "保存";
      actions.append(cancel, ok);
      card.append(h3, p, input, actions);
      root.append(card);
      const finish = (result) => {
        root.hidden = true;
        resolve(result);
      };
      cancel.addEventListener("click", () => finish(null));
      ok.addEventListener("click", () => finish(input.value.trim() || null));
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          finish(input.value.trim() || null);
        }
        if (event.key === "Escape") finish(null);
      });
      root.onclick = (event) => {
        if (event.target === root) finish(null);
      };
      input.focus();
      input.select();
    });
  }

  /* ---------------------------------------------------------- agent modal */
  function agentFilePicker(title, accept, multiple) {
    const field = document.createElement("div");
    field.className = "agent-file-field";
    const label = document.createElement("span");
    label.textContent = title;
    label.style.cssText = "display:block;margin-bottom:5px;font-size:10px;color:var(--muted);";
    const pick = document.createElement("label");
    pick.className = "file-pick";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.multiple = multiple;
    const hint = document.createElement("span");
    hint.textContent = multiple ? "点击选择脚本文件（可多选）" : "点击选择 Agent Markdown";
    pick.append(input, hint);
    const chips = document.createElement("div");
    chips.className = "file-chips";
    field.append(label, pick, chips);
    return { field, input, chips };
  }

  function showFileChips(chips, files) {
    chips.innerHTML = "";
    for (const file of files) {
      const chip = document.createElement("span");
      chip.className = "file-chip";
      chip.textContent = `${file.name} · ${Math.max(1, Math.round(file.size / 1024))}KB`;
      chips.append(chip);
    }
  }

  function showAgentTest(agent, existingRoot = null) {
    const root = existingRoot || modalShell();
    root.hidden = false;
    root.onclick = (event) => {
      if (event.target === root) root.hidden = true;
    };
    const card = document.createElement("div");
    card.className = "modal-card agent-modal";
    card.innerHTML = `
      <h3>测试「${escapeHtml(agent.name)}」</h3>
      <p>检查入口脚本、本地命令、实际执行、stdout 和产物下载。测试会真正运行脚本，并保存产物。</p>
    `;
    const questionField = document.createElement("label");
    questionField.className = "agent-test-question";
    questionField.innerHTML = "<span>测试输入</span>";
    const questionInput = document.createElement("input");
    questionInput.type = "text";
    questionInput.value = "测试执行：请输出 OK";
    questionInput.maxLength = 500;
    questionField.append(questionInput);
    const actions = document.createElement("div");
    actions.className = "agent-modal-actions";
    actions.style.marginTop = "10px";
    const done = document.createElement("button");
    done.type = "button";
    done.className = "btn ghost";
    done.textContent = "完成";
    done.addEventListener("click", () => (root.hidden = true));
    const run = document.createElement("button");
    run.type = "button";
    run.className = "btn primary";
    run.style.height = "36px";
    run.style.fontSize = "11.5px";
    run.textContent = "运行测试";
    actions.append(done, run);
    const result = document.createElement("div");
    result.className = "agent-test-result";
    result.hidden = true;
    card.append(questionField, actions, result);
    root.replaceChildren(card);
    const showChecks = (data) => {
      const checks = document.createElement("div");
      checks.className = "test-checks";
      for (const check of data.checks || []) {
        const row = document.createElement("div");
        row.className = "test-check " + (check.ok ? "ok" : "fail");
        row.innerHTML = `<span class="test-dot"></span><strong></strong><span></span>`;
        row.querySelector("strong").textContent = check.label;
        row.querySelector("span:last-child").textContent = check.detail || "";
        checks.append(row);
      }
      const answer = document.createElement("div");
      answer.className = "test-answer";
      answer.textContent = data.answer || "(无回答)";
      result.append(checks, answer);
      if (data.execution?.stdout) {
        const pre = document.createElement("pre");
        pre.textContent = data.execution.stdout;
        result.append(pre);
      }
      if (data.execution?.stderr) {
        const pre = document.createElement("pre");
        pre.className = "stderr";
        pre.textContent = data.execution.stderr;
        result.append(pre);
      }
      if (Array.isArray(data.artifacts) && data.artifacts.length) {
        const box = document.createElement("div");
        box.className = "test-artifacts";
        const label = document.createElement("div");
        label.className = "execution-label";
        label.textContent = "可下载产物";
        box.append(label);
        for (const artifact of data.artifacts) {
          const link = document.createElement("a");
          link.href = artifact.url || "#";
          link.className = "execution-artifact";
          link.innerHTML = `<svg class="icon"><use href="#i-book"/></svg><span></span><em></em>`;
          link.querySelector("span").textContent = artifact.name || "artifact";
          link.querySelector("em").textContent = formatBytes(artifact.size);
          box.append(link);
        }
        result.append(box);
      }
      result.hidden = false;
    };
    run.addEventListener("click", async () => {
      const question = questionInput.value.trim();
      if (!question) {
        toast("请填写测试输入", "error");
        return;
      }
      run.disabled = true;
      run.textContent = "测试中…";
      result.hidden = true;
      result.replaceChildren();
      try {
        const data = await api(`/api/agents/${encodeURIComponent(agent.id)}/test`, {
          method: "POST",
          body: { question },
        });
        showChecks(data);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        run.disabled = false;
        run.textContent = "运行测试";
      }
    });
    questionInput.focus();
  }

  function openAgentConfig(agent) {
    const root = modalShell();
    root.onclick = (event) => {
      if (event.target === root) root.hidden = true;
    };
    const card = document.createElement("div");
    card.className = "modal-card agent-modal";
    card.innerHTML = `
      <h3>执行配置</h3>
      <p>为「${escapeHtml(agent.name)}」指定入口脚本。配置后 Agent 模式会执行脚本，并把输出文件保存为可下载产物。</p>
    `;
    const form = document.createElement("form");
    form.className = "agent-form";
    const row = document.createElement("div");
    row.className = "agent-form-row";
    const entryField = document.createElement("label");
    entryField.innerHTML = "<span>入口脚本</span>";
    const entryInput = document.createElement("input");
    entryInput.type = "text";
    entryInput.placeholder = "如 scripts/run.py；留空则为 Prompt Skill";
    entryInput.value = agent.entrypoint || "";
    entryInput.maxLength = 200;
    const suggestions = document.createElement("datalist");
    suggestions.id = "agent-entrypoints";
    for (const name of agent.files || []) {
      if (/\.(py|sh|js|mjs)$/i.test(name)) {
        const option = document.createElement("option");
        option.value = name;
        suggestions.append(option);
      }
    }
    entryInput.setAttribute("list", "agent-entrypoints");
    entryField.append(entryInput, suggestions);
    const runtimeField = document.createElement("label");
    runtimeField.innerHTML = "<span>Runtime</span>";
    const runtimeSelect = document.createElement("select");
    runtimeSelect.innerHTML = '<option value="python">python</option><option value="bash">bash</option><option value="node">node</option>';
    runtimeSelect.value = agent.runtime || "python";
    runtimeField.append(runtimeSelect);
    row.append(entryField, runtimeField);
    const timeoutField = document.createElement("label");
    timeoutField.innerHTML = "<span>超时（秒，最大 15）</span>";
    const timeoutInput = document.createElement("input");
    timeoutInput.type = "number";
    timeoutInput.min = "1";
    timeoutInput.max = "15";
    timeoutInput.step = "1";
    timeoutInput.value = String(Math.round(agent.timeout || 10));
    timeoutField.append(timeoutInput);
    const error = document.createElement("div");
    error.className = "agent-form-error";
    error.hidden = true;
    const note = document.createElement("div");
    note.className = "agent-note";
    note.textContent = "脚本在临时目录执行，stdout/stderr 会展示；写入 out/ 的文件或新生成的文件会保存为产物。";
    const actions = document.createElement("div");
    actions.className = "agent-modal-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn ghost";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => (root.hidden = true));
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "btn primary";
    save.style.height = "36px";
    save.style.fontSize = "11.5px";
    save.textContent = "保存";
    actions.append(cancel, save);
    form.append(row, timeoutField, error, note, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.hidden = true;
      save.disabled = true;
      save.textContent = "保存中…";
      try {
        const data = await api(`/api/agents/${encodeURIComponent(agent.id)}`, {
          method: "PATCH",
          body: {
            entrypoint: entryInput.value.trim(),
            runtime: runtimeSelect.value,
            timeout: Number(timeoutInput.value) || 10,
          },
        });
        state.agents = state.agents.map((a) => (a.id === agent.id ? data.agent : a));
        if (state.agentId === agent.id) selectAgent(agent.id, { silent: true });
        root.hidden = true;
        openAgentManager();
        toast("执行配置已保存", "success");
      } catch (err) {
        showError(error, err.message);
      } finally {
        save.disabled = false;
        save.textContent = "保存";
      }
    });
    card.append(form);
    root.append(card);
    entryInput.focus();
  }

  function openAgentManager(options = {}) {
    closeAgentMenu();
    closeAgentNav();
    const root = modalShell();
    root.onclick = (event) => {
      if (event.target === root) root.hidden = true;
    };
    const card = document.createElement("div");
    card.className = "modal-card agent-modal";
    card.innerHTML = `
      <h3>Agent / Skill 管理</h3>
      <p>从 GitHub 自动导入，或上传 Markdown 定义和参考脚本。Markdown 可用 frontmatter 指定
        <code>entrypoint</code>、<code>runtime</code>、<code>timeout</code>。</p>
    `;
    const githubBox = document.createElement("div");
    githubBox.className = "github-import";
    githubBox.innerHTML = `
      <div class="github-import-title">从 GitHub 导入 Skill / Agent</div>
      <div class="github-import-help">粘贴仓库或目录地址，自动读取 SKILL.md / AGENT.md 和文本文件（最多 250 个 / 5MB）。私有仓库需要服务端配置 GITHUB_TOKEN。</div>
      <div class="github-row">
        <input type="text" id="github-url" placeholder="https://github.com/anthropics/skills/tree/main/skills/pdf">
        <button type="button" id="github-import-btn">导入</button>
      </div>
      <div class="github-extra">
        <input type="text" id="github-entrypoint" placeholder="入口脚本（可选），如 scripts/run.py">
        <select id="github-kind">
          <option value="">自动识别 Agent / Skill</option>
          <option value="agent">Agent</option>
          <option value="skill">Skill</option>
        </select>
      </div>
      <div id="github-error" class="agent-form-error" hidden></div>
    `;
    const githubUrl = githubBox.querySelector("#github-url");
    const githubEntry = githubBox.querySelector("#github-entrypoint");
    const githubKind = githubBox.querySelector("#github-kind");
    const githubBtn = githubBox.querySelector("#github-import-btn");
    const githubError = githubBox.querySelector("#github-error");
    githubBtn.addEventListener("click", async () => {
      githubError.hidden = true;
      const url = githubUrl.value.trim();
      if (!url) {
        showError(githubError, "请填写 GitHub 地址");
        return;
      }
      githubBtn.disabled = true;
      githubBtn.textContent = "导入中…";
      try {
        const data = await api("/api/agents/import/github", {
          method: "POST",
          body: {
            url,
            entrypoint: githubEntry.value.trim() || null,
            kind: githubKind.value || null,
          },
        });
        state.agents.unshift(data.agent);
        selectAgent(data.agent.id, { silent: true });
        toast(`已从 GitHub 导入 ${agentKind(data.agent)}「${data.agent.name}」`, "success");
        showAgentTest(data.agent, root);
      } catch (err) {
        showError(githubError, err.message);
      } finally {
        githubBtn.disabled = false;
        githubBtn.textContent = "导入";
      }
    });

    const form = document.createElement("form");
    form.className = "agent-form";

    const row = document.createElement("div");
    row.className = "agent-form-row";
    const nameField = document.createElement("label");
    nameField.innerHTML = "<span>名称（可选）</span>";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "默认取 Markdown 一级标题";
    nameInput.maxLength = 60;
    nameField.append(nameInput);
    const descField = document.createElement("label");
    descField.innerHTML = "<span>描述（可选）</span>";
    const descInput = document.createElement("input");
    descInput.type = "text";
    descInput.placeholder = "这个 Agent 负责什么";
    descInput.maxLength = 200;
    descField.append(descInput);
    row.append(nameField, descField);

    const row2 = document.createElement("div");
    row2.className = "agent-form-row";
    const kindField = document.createElement("label");
    kindField.innerHTML = "<span>类型</span>";
    const kindSelect = document.createElement("select");
    kindSelect.innerHTML = '<option value="agent">Agent（可配置入口脚本）</option><option value="skill">Skill（指令 / 参考脚本）</option>';
    kindField.append(kindSelect);
    const entryField = document.createElement("label");
    entryField.innerHTML = "<span>入口脚本（可选）</span>";
    const entryInput = document.createElement("input");
    entryInput.type = "text";
    entryInput.placeholder = "如 scripts/run.py；Agent 默认取第一个脚本";
    entryInput.maxLength = 200;
    entryField.append(entryInput);
    row2.append(kindField, entryField);

    const mdPicker = agentFilePicker("Agent Markdown", ".md,.markdown,.txt", false);
    const scriptPicker = agentFilePicker(
      "参考脚本",
      ".py,.sh,.js,.mjs,.ts,.txt,.json,.md,.html,.css,.svg,.xml,.yaml,.yml,.toml,.csv",
      true
    );
    let mdFile = null;
    let scriptFiles = [];
    mdPicker.input.addEventListener("change", () => {
      mdFile = mdPicker.input.files?.[0] || null;
      showFileChips(mdPicker.chips, mdFile ? [mdFile] : []);
    });
    scriptPicker.input.addEventListener("change", () => {
      scriptFiles = Array.from(scriptPicker.input.files || []).slice(0, 250);
      showFileChips(scriptPicker.chips, scriptFiles);
    });

    const error = document.createElement("div");
    error.className = "agent-form-error";
    error.hidden = true;
    const note = document.createElement("div");
    note.className = "agent-note";
    note.textContent =
      "脚本会在临时目录、受限环境和超时限制下执行；这不是强沙箱，请只导入你信任的脚本。";
    const actions = document.createElement("div");
    actions.className = "agent-modal-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn ghost";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => (root.hidden = true));
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn primary";
    submit.style.height = "36px";
    submit.style.fontSize = "11.5px";
    submit.textContent = "导入并选择";
    actions.append(cancel, submit);

    form.append(row, row2, mdPicker.field, scriptPicker.field, error, note, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.hidden = true;
      if (!mdFile) {
        showError(error, "请选择 Agent Markdown 文件");
        return;
      }
      if (scriptFiles.length > 250) {
        showError(error, "最多导入 250 个文件");
        return;
      }
      submit.disabled = true;
      submit.textContent = "导入中…";
      try {
        if (mdFile.size > 1_000_000) throw new Error("Markdown 不能超过 1MB");
        const markdown = await mdFile.text();
        const files = [];
        for (const file of scriptFiles) {
          if (file.size > 512_000) throw new Error(`${file.name} 超过 512KB`);
          files.push({ name: file.name, content: await file.text() });
        }
        const data = await api("/api/agents", {
          method: "POST",
          body: {
            name: nameInput.value.trim() || null,
            description: descInput.value.trim() || null,
            kind: kindSelect.value,
            entrypoint: entryInput.value.trim() || null,
            markdown,
            files,
          },
        });
        state.agents.unshift(data.agent);
        selectAgent(data.agent.id, { silent: true });
        toast(`已导入 ${agentKind(data.agent)}「${data.agent.name}」`, "success");
        showAgentTest(data.agent, root);
      } catch (err) {
        showError(error, err.message);
      } finally {
        submit.disabled = false;
        submit.textContent = "导入并选择";
      }
    });

    card.append(githubBox, form);

    const existing = document.createElement("div");
    existing.className = "agent-existing";
    const existingHead = document.createElement("h4");
    existingHead.textContent = `已导入的 Agent / Skill · ${state.agents.length}`;
    existing.append(existingHead);
    if (!state.agents.length) {
      const empty = document.createElement("p");
      empty.style.cssText = "font-size:10px;color:var(--faint);";
      empty.textContent = "还没有导入 Agent / Skill。默认 Agent 会使用内置或外部 Agent 服务。";
      existing.append(empty);
    }
    for (const agent of state.agents) {
      const item = document.createElement("div");
      item.className = "agent-existing-item";
      item.innerHTML = `
        <span class="agent-menu-icon"><svg class="icon"><use href="#i-terminal"/></svg></span>
        <span class="agent-menu-meta">
          <span class="agent-menu-name"></span>
          <span class="agent-menu-desc"></span>
        </span>
        <button type="button" class="icon-btn test-agent" title="测试" aria-label="测试">
          <svg class="icon"><use href="#i-check"/></svg>
        </button>
        <button type="button" class="icon-btn config-agent" title="执行配置" aria-label="执行配置">
          <svg class="icon"><use href="#i-edit"/></svg>
        </button>
        <button type="button" class="icon-btn delete-agent" title="删除" aria-label="删除">
          <svg class="icon"><use href="#i-trash"/></svg>
        </button>
      `;
      item.querySelector(".agent-menu-name").textContent = agent.name;
      item.querySelector(".agent-menu-desc").textContent =
        `${agentKind(agent)} · ${agent.files?.length || 0} 个文件 · `
        + (agent.entrypoint || (agent.kind === "skill" ? "Prompt Skill（需要模型）" : "无入口脚本"))
        + (agent.source_url ? ` · ${agent.source_url}` : "");
      item.querySelector(".test-agent").addEventListener("click", () => {
        root.hidden = true;
        showAgentTest(agent);
      });
      item.querySelector(".config-agent").addEventListener("click", () => {
        root.hidden = true;
        openAgentConfig(agent);
      });
      item.querySelector(".delete-agent").addEventListener("click", async () => {
        const ok = await confirmDialog(
          "删除这个 Agent？",
          `「${agent.name}」的定义和参考脚本会被删除，已经产生的历史回答不受影响。`,
          "删除"
        );
        if (!ok) {
          openAgentManager();
          return;
        }
        try {
          await api(`/api/agents/${encodeURIComponent(agent.id)}`, { method: "DELETE" });
          state.agents = state.agents.filter((a) => a.id !== agent.id);
          if (state.agentId === agent.id) selectAgent(null, { silent: true });
          openAgentManager();
          toast("Agent 已删除");
        } catch (err) {
          toast(err.message, "error");
        }
      });
      existing.append(item);
    }
    card.append(existing);
    root.append(card);
    if (options.focusGithub) githubUrl.focus();
    else nameInput.focus();
  }

  /* --------------------------------------------------------------- events */
  function bindEvents() {
    $$(".auth-tab").forEach((tab) =>
      tab.addEventListener("click", () => setAuthView(tab.dataset.authView))
    );
    $("#login-form").addEventListener("submit", handleLogin);
    $("#register-form").addEventListener("submit", handleRegister);

    $("#kb-switcher").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleKbMenu();
    });
    $("#new-chat").addEventListener("click", newChat);
    $("#conversation-search").addEventListener("input", (event) => {
      state.search = event.target.value;
      renderConversations();
    });
    $("#user-menu-btn").addEventListener("click", (event) => {
      event.stopPropagation();
      const menu = $("#user-menu");
      const open = menu.hidden;
      menu.hidden = !open;
      $("#user-menu-btn").setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".user-card")) {
        $("#user-menu").hidden = true;
        $("#user-menu-btn").setAttribute("aria-expanded", "false");
      }
      if (!event.target.closest(".kb-switcher-wrap")) closeKbMenu();
      if (!event.target.closest(".agent-picker") && !event.target.closest("#agent-menu")) {
        closeAgentMenu();
      }
    });
    $("#theme-toggle").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      applyTheme(next);
    });
    $("#logout-btn").addEventListener("click", logout);

    $("#sidebar-open").addEventListener("click", openMobileSidebar);
    $("#sidebar-close").addEventListener("click", closeMobileSidebar);
    $("#sidebar-backdrop").addEventListener("click", closeMobileSidebar);
    $("#panel-close").addEventListener("click", closePanel);
    $("#panel-toggle").addEventListener("click", togglePanel);
    $("#panel-backdrop").addEventListener("click", closePanel);

    $$(".mode-switch button").forEach((btn) =>
      btn.addEventListener("click", () => setMode(btn.dataset.mode))
    );
    $$(".composer-switch button").forEach((btn) =>
      btn.addEventListener("click", () => setMode(btn.dataset.composerMode))
    );
    $("#agent-picker").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleAgentMenu();
    });
    $("#live-toggle").addEventListener("click", () => {
      if (state.mode !== "rag") return;
      state.live = !state.live;
      updateComposer();
    });
    $("#agent-nav-toggle").addEventListener("click", (event) => {
      event.stopPropagation();
      toggleAgentNav();
    });
    $("#title-edit").addEventListener("click", async () => {
      const conv = state.conversations.find((c) => c.id === state.activeId);
      if (conv) await renameConversation(conv);
    });
    $("#clear-conversation-cta").addEventListener("click", clearCurrentConversation);

    const input = $("#message-input");
    let compositionEndedAt = 0;
    input.addEventListener("compositionend", () => {
      compositionEndedAt = performance.now();
    });
    input.addEventListener("input", () => {
      resizeInput();
      updateComposer();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      // macOS/Windows IMEs can report isComposing=false for the Enter that
      // confirms a candidate; keyCode 229 and the recent compositionend guard
      // keep that Enter inside the IME instead of submitting the message.
      const justFinishedComposition = performance.now() - compositionEndedAt < 120;
      if (event.isComposing || event.keyCode === 229 || justFinishedComposition) {
        return;
      }
      if (!event.shiftKey) {
        event.preventDefault();
        submitMessage();
      }
    });
    $("#composer").addEventListener("submit", (event) => {
      event.preventDefault();
      // A form submit can also be triggered by the Enter that confirms an IME
      // candidate in some browsers. Keep that key inside the IME.
      if (performance.now() - compositionEndedAt < 120) return;
      submitMessage();
    });
    $("#send-btn").addEventListener("click", (event) => {
      event.preventDefault();
      submitMessage();
    });
    $("#cancel-btn").addEventListener("click", async () => {
      if (!state.sending || !state.activeId) return;
      const btn = $("#cancel-btn");
      btn.disabled = true;
      btn.textContent = "取消中…";
      try {
        const data = await api(`/api/conversations/${encodeURIComponent(state.activeId)}/cancel`, {
          method: "POST",
        });
        toast(data.cancelled ? "已请求取消" : "当前没有运行中的任务");
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
        btn.textContent = "停止";
      }
    });

    $$(".panel-tab").forEach((tab) =>
      tab.addEventListener("click", () => switchPanelView(tab.dataset.view))
    );

    document.addEventListener("click", (event) => {
      const cite = event.target.closest(".citation-ref");
      if (cite) {
        const index = Number(cite.dataset.cite);
        const msgEl = cite.closest(".msg");
        const owner = state.messages.find((m) => m.id === msgEl?.dataset.id);
        const citations = owner?.citations?.length ? owner.citations : state.citations;
        const citation = citations.find((c) => Number(c.index) === index);
        if (citation) {
          state.citations = citations;
          renderCitations();
          switchPanelView("citations");
          openPanel();
          setTimeout(() => {
            const cards = $$(".citation-card");
            const card = cards.find((c) => c.querySelector(".citation-index")?.textContent === String(index));
            card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
            card?.classList.add("flash");
          }, 60);
        } else {
          toast(`引用 ${index} 不在当前回答中`);
        }
      }
    });

    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (!$("#app-shell").classList.contains("is-hidden")) newChat();
      }
      if (event.key === "Escape") {
        $("#modal-root").hidden = true;
        $("#entity-sheet").hidden = true;
        closePanel();
        closeMobileSidebar();
        closeKbMenu();
        closeAgentMenu();
        closeAgentNav();
      }
    });

    window.addEventListener("resize", () => {
      if (!isMobilePanel()) {
        $("#panel-backdrop").hidden = true;
        $("#insight-panel").classList.remove("open");
      }
    });

    bindGraphInteractions();
  }

  // Delegated fallback: the memory entry is also reachable if an earlier
  // binding throws or a stale app.js is cached.
  document.addEventListener("click", (event) => {
    if (event.target.closest("#kb-docs-btn")) {
      event.preventDefault();
      openKbDocs();
      return;
    }
    if (event.target.closest("#admin-btn")) {
      event.preventDefault();
      openAdminUsers();
      return;
    }
    if (event.target.closest("#connector-btn")) {
      event.preventDefault();
      openConnectorManager();
      return;
    }
    if (!event.target.closest("#memory-btn")) return;
    event.preventDefault();
    openMemoryManager();
  });

  document.addEventListener("DOMContentLoaded", boot);
})();
