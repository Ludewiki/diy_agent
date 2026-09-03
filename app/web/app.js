(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    weekday: "short",
  });
  const shortDateFormatter = new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  });

  const defaultWarnings = [
    "天气仅覆盖近期预报，建议出发前 48 小时重新运行。",
    "预算是规划偏好，不代表酒店、交通或门票实时报价。",
    "地图瓦片不可用时，自动降级为坐标投影路线。",
  ];

  const state = {
    eventSource: null,
    terminal: false,
    runId: null,
    sessionId: null,
    traceId: null,
    weather: null,
    plan: null,
    map: null,
    mapLayer: null,
    tileLayer: null,
    currentDay: 0,
    warnings: new Set(defaultWarnings),
    extraSources: [],
    user: null,
    csrfToken: null,
    authMode: "login",
    historyPage: 1,
    historyTotal: 0,
    historyPageSize: 8,
    initialSessionRestored: false,
    runActive: false,
    sessionItems: [],
  };

  function parseDate(value) {
    const date = new Date(String(value) + "T12:00:00");
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDate(value, formatter = dateFormatter) {
    const date = parseDate(value);
    return date ? formatter.format(date) : String(value || "—");
  }

  function setStatus(label, tone) {
    const node = $("#run-status");
    node.textContent = label;
    node.className = "status-chip status-" + tone;
  }

  function setButtonRunning(running) {
    state.runActive = running;
    const button = $("#submit-button");
    button.disabled = running;
    button.firstElementChild.textContent = running ? "Agent 正在规划…" : "生成我的旅行方案";
    updateComposerState();
  }

  function updateComposerState() {
    const canFollowUp = Boolean(state.user && state.sessionId && !state.runActive);
    const input = $("#follow-up-input");
    const submit = $("#follow-up-submit");
    input.disabled = !canFollowUp;
    submit.disabled = !canFollowUp;
    $$("[data-follow-up]").forEach((button) => {
      button.disabled = !canFollowUp;
    });
    if (state.runActive) {
      $("#composer-hint").textContent = "当前 Run 执行中，完成后可继续追问";
      submit.firstElementChild.textContent = "Agent 执行中…";
    } else if (state.sessionId) {
      $("#composer-hint").textContent = "消息会发送到当前 Session · Ctrl/⌘ + Enter";
      submit.firstElementChild.textContent = "发送追问";
    } else {
      $("#composer-hint").textContent = "选择一个历史 Session，或先创建新旅行";
      submit.firstElementChild.textContent = "发送追问";
    }
  }

  function setStage(name, status, detail) {
    const node = $('[data-stage="' + name + '"]');
    if (!node) return;
    node.classList.remove("is-active", "is-done");
    if (status === "active") node.classList.add("is-active");
    if (status === "done") node.classList.add("is-done");
    if (detail) node.querySelector("span").textContent = detail;
  }

  function finishAllStages() {
    $$("#progress-list li").forEach((node) => {
      node.classList.remove("is-active");
      node.classList.add("is-done");
    });
  }

  function resetProgress() {
    const details = {
      queued: "正在提交",
      weather: "Open-Meteo",
      guide: "Wikivoyage",
      route: "OpenRouteService",
      complete: "等待 Agent",
    };
    $$("#progress-list li").forEach((node) => {
      node.classList.remove("is-active", "is-done");
      node.querySelector("span").textContent = details[node.dataset.stage];
    });
    $("#context-tokens").textContent = "0 / 12,000 tokens";
    $("#context-history").textContent = "历史 0 条";
    $("#context-summary").textContent = "摘要未使用";
  }

  function showError(message) {
    $("#error-message").textContent = message || "发生未知错误，请稍后重试。";
    $("#error-banner").classList.remove("is-hidden");
  }

  function hideError() {
    $("#error-banner").classList.add("is-hidden");
    $("#error-message").textContent = "";
  }

  function addWarning(message) {
    if (message) state.warnings.add(String(message));
    renderWarnings();
  }

  function renderWarnings() {
    const list = $("#warning-list");
    list.replaceChildren();
    Array.from(state.warnings).slice(0, 10).forEach((warning) => {
      list.append(create("li", "", warning));
    });
  }

  function resetWorkspace(city) {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
    state.terminal = false;
    state.runId = null;
    state.sessionId = null;
    state.traceId = null;
    state.weather = null;
    state.plan = null;
    state.currentDay = 0;
    state.warnings = new Set(defaultWarnings);
    state.extraSources = [];
    renderConversation([]);
    updateComposerState();
    $("#workspace").classList.remove("is-idle");
    $("#journey-title").textContent = city + " · 正在生成";
    $("#run-reference").textContent = "RUN —";
    $("#trace-id").textContent = "—";
    $("#weather-location").textContent = "等待天气 Tool 返回";
    $("#itinerary-summary").textContent = "等待路线生成";
    $("#answer-panel").classList.add("is-hidden");
    $("#final-answer").textContent = "";
    $("#day-tabs").replaceChildren();
    $("#itinerary-days").replaceChildren(
      create("div", "empty-copy", "Agent 正在准备天气、攻略与路线数据。")
    );
    $("#route-summary").replaceChildren();
    $("#route-summary").classList.remove("is-visible");
    hideError();
    resetProgress();
    setStage("queued", "active", "正在创建 Session 与 Run");
    setStatus("正在连接", "running");
    renderWeatherSkeleton();
    renderSources();
    renderWarnings();
    clearMap();
  }

  function renderWeatherSkeleton() {
    const container = $("#weather-candidates");
    container.replaceChildren();
    for (let index = 0; index < 3; index += 1) {
      const card = create("article", "weather-placeholder");
      card.append(
        create("span", "skeleton skeleton-short"),
        create("span", "skeleton"),
        create("span", "skeleton skeleton-medium")
      );
      container.append(card);
    }
  }

  function renderConversation(messages) {
    const container = $("#conversation-list");
    container.replaceChildren();
    if (!messages.length) {
      const empty = create("div", "conversation-empty");
      empty.append(
        create("strong", "", "先创建一次旅行规划"),
        create("span", "", "完成后可在这里继续追问、调整局部行程，并复用本次会话上下文。")
      );
      container.append(empty);
      $("#conversation-memory").textContent = "同一 Session";
      return;
    }
    messages.forEach((message) => appendConversationMessage(message, false));
    $("#conversation-memory").textContent = "短期记忆 · " + messages.length + " 条";
    container.scrollTop = container.scrollHeight;
  }

  function appendConversationMessage(message, pending = false) {
    const container = $("#conversation-list");
    const empty = container.querySelector(".conversation-empty");
    if (empty) empty.remove();
    const isUser = String(message.role || "").toUpperCase() === "USER";
    const bubble = create(
      "article",
      "message-bubble " + (isUser ? "is-user" : "is-assistant") + (pending ? " is-pending" : "")
    );
    const heading = create("header");
    heading.append(
      create("strong", "", isUser ? "YOU" : "ATLAS"),
      create("span", "", pending ? "发送中" : formatSessionTime(message.created_at))
    );
    bubble.append(heading, create("p", "", message.content || ""));
    container.append(bubble);
    container.scrollTop = container.scrollHeight;
  }

  async function requestJson(url, options) {
    const requestOptions = options || {};
    const method = String(requestOptions.method || "GET").toUpperCase();
    const headers = {
      "Content-Type": "application/json",
      ...(requestOptions.headers || {}),
    };
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrfToken) {
      headers["X-CSRF-Token"] = state.csrfToken;
    }
    const response = await fetch(url, {
      ...requestOptions,
      method,
      credentials: "same-origin",
      headers,
    });
    let body = {};
    try {
      body = await response.json();
    } catch (_) {
      body = {};
    }
    if (!response.ok) {
      const error = new Error(body.message || "请求失败（HTTP " + response.status + "）");
      error.payload = body;
      throw error;
    }
    return { body, response };
  }

  function buildPrompt(formData) {
    const city = String(formData.get("city") || "").trim();
    const days = String(formData.get("days") || "3");
    const budget = String(formData.get("budget") || "适中预算");
    const interests = formData.getAll("interest").map(String);
    const notes = String(formData.get("notes") || "").trim();
    const interestText = interests.length ? interests.join("、") : "经典城市体验";
    const noteText = notes ? "补充偏好：" + notes + "。" : "";
    return (
      "请为我规划近期去" + city + "连续游玩" + days + "天的旅行。" +
      "我的兴趣是" + interestText + "，预算偏好是" + budget + "。" +
      noteText +
      "请严格先筛选最佳连续天气窗口，再检索攻略、规划每日景点顺序，" +
      "并明确列出数据来源、降级提示和最终行程。"
    );
  }

  async function submitJourney(event) {
    event.preventDefault();
    if (!state.user) {
      openAuthDialog();
      return;
    }
    const formData = new FormData(event.currentTarget);
    const city = String(formData.get("city") || "").trim();
    const prompt = buildPrompt(formData);
    if (!city) return;
    resetWorkspace(city);
    setButtonRunning(true);
    $("#workspace").scrollIntoView({ behavior: "smooth", block: "start" });

    try {
      const sessionResult = await requestJson("/v1/sessions", {
        method: "POST",
        body: JSON.stringify({ title: city + "旅行方案" }),
      });
      state.sessionId = sessionResult.body.id;
      setStage("queued", "active", "Session 已创建，正在入队");

      const runResult = await requestJson(
        "/v1/sessions/" + state.sessionId + "/messages",
        {
          method: "POST",
          body: JSON.stringify({
            content: prompt,
            planning_context: {
              city,
              trip_days: Number(formData.get("days") || 3),
              interests: formData.getAll("interest").map(String),
              budget: String(formData.get("budget") || ""),
              additional_preferences: String(formData.get("notes") || "").trim() || null,
            },
          }),
        }
      );
      state.runId = runResult.body.run.id;
      state.traceId = runResult.response.headers.get("X-Trace-ID");
      $("#run-reference").textContent = "RUN " + state.runId.slice(0, 8).toUpperCase();
      $("#trace-id").textContent = state.traceId || "等待 Trace";
      setStatus("Run 已入队", "running");
      await loadMessageTimeline(state.sessionId);
      openEventStream(runResult.body.events_url);
      loadSessionHistory({ restoreLatest: false });
    } catch (error) {
      setStatus("提交失败", "error");
      setButtonRunning(false);
      showError(error.message);
    }
  }

  async function submitFollowUp(event) {
    event.preventDefault();
    const input = $("#follow-up-input");
    const content = input.value.trim();
    if (!content || !state.sessionId || state.runActive) return;

    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
    state.terminal = false;
    state.runId = null;
    state.traceId = null;
    hideError();
    resetProgress();
    setStage("queued", "active", "正在把追问加入当前 Session");
    setStatus("追问入队中", "running");
    setButtonRunning(true);
    input.value = "";
    appendConversationMessage(
      { role: "USER", content, created_at: new Date().toISOString() },
      true
    );

    try {
      const runResult = await requestJson(
        "/v1/sessions/" + state.sessionId + "/messages",
        {
          method: "POST",
          body: JSON.stringify({ content }),
        }
      );
      state.runId = runResult.body.run.id;
      state.traceId = runResult.response.headers.get("X-Trace-ID");
      $("#run-reference").textContent = "RUN " + state.runId.slice(0, 8).toUpperCase();
      $("#trace-id").textContent = state.traceId || "等待 Trace";
      setStatus("追问已入队", "running");
      renderRunOutput(runResult.body.run.output);
      await loadMessageTimeline(state.sessionId);
      openEventStream(runResult.body.events_url);
      loadSessionHistory({ restoreLatest: false });
    } catch (error) {
      input.value = content;
      setStatus("追问失败", "error");
      setButtonRunning(false);
      showError(error.message);
      await loadMessageTimeline(state.sessionId);
    }
  }

  function openEventStream(url) {
    if (state.eventSource) state.eventSource.close();
    const stream = new EventSource(url);
    state.eventSource = stream;
    const eventTypes = [
      "RUN_QUEUED",
      "RUN_STARTED",
      "RUN_RECLAIMED",
      "RUN_RETRY_STARTED",
      "CONTEXT_PREPARED",
      "CONTEXT_LIMIT_REACHED",
      "AGENT_THINKING",
      "TOOL_STARTED",
      "TOOL_SUCCEEDED",
      "TOOL_FAILED",
      "RUN_RETRY_SCHEDULED",
      "RUN_SUCCEEDED",
      "RUN_FAILED",
      "RUN_CANCELLED",
    ];
    eventTypes.forEach((eventType) => {
      stream.addEventListener(eventType, (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (_) {
          return;
        }
        handleProgressEvent(payload);
      });
    });
    stream.onopen = () => setStatus("Agent 执行中", "running");
    stream.onerror = () => {
      if (!state.terminal) setStatus("SSE 重连中", "running");
    };
  }

  function handleProgressEvent(event) {
    const type = event.type;
    const data = event.data || {};
    if (type === "RUN_QUEUED") {
      setStage("queued", "done", "任务已持久化");
    } else if (["RUN_STARTED", "RUN_RECLAIMED", "RUN_RETRY_STARTED"].includes(type)) {
      setStage("queued", "done", type === "RUN_RECLAIMED" ? "任务已由其他 Worker 回收" : "Worker 已领取");
      setStage("weather", "active", "Agent 正在选择工具");
    } else if (type === "CONTEXT_PREPARED") {
      const used = Number(data.estimated_input_tokens || 0).toLocaleString("zh-CN");
      const limit = Number(data.max_input_tokens || 0).toLocaleString("zh-CN");
      $("#context-tokens").textContent = used + " / " + limit + " tokens";
      $("#context-history").textContent = "历史 " + Number(data.history_messages_used || 0) + " 条";
      $("#context-summary").textContent = data.summary_present
        ? (data.summary_updated ? "摘要已更新" : "摘要已复用")
        : "摘要未使用";
      if (data.over_budget) {
        addWarning("当前问题与最近上下文超过估算预算；Agent 已优先保留当前问题和最近一轮。");
      }
    } else if (type === "CONTEXT_LIMIT_REACHED") {
      addWarning(
        String(data.limit_type || "Agent") + " 调用已达到本次 Run 的安全上限。"
      );
    } else if (type === "TOOL_STARTED") {
      handleToolStarted(data.tool_name);
    } else if (type === "TOOL_SUCCEEDED") {
      handleToolCompleted(data.tool_name, true);
    } else if (type === "TOOL_FAILED") {
      handleToolCompleted(data.tool_name, false);
    } else if (type === "RUN_RETRY_SCHEDULED") {
      setStatus("等待重试", "running");
      addWarning("本次执行遇到可恢复错误，Worker 已安排重试。");
    } else if (type === "RUN_SUCCEEDED") {
      state.terminal = true;
      state.eventSource.close();
      finishAllStages();
      setStatus("规划完成", "success");
      setButtonRunning(false);
      fetchFinalRun();
      loadSessionHistory({ restoreLatest: false });
    } else if (type === "RUN_FAILED" || type === "RUN_CANCELLED") {
      state.terminal = true;
      state.eventSource.close();
      setStatus(type === "RUN_CANCELLED" ? "已取消" : "规划失败", "error");
      setButtonRunning(false);
      showError(data.message || data.error_message || "Agent 未能生成完整行程。");
      fetchFinalRun();
      loadSessionHistory({ restoreLatest: false });
    }
  }

  function handleToolStarted(name) {
    if (name === "find_best_weather_window") {
      setStage("weather", "active", "正在比较连续天气窗口");
    } else if (name === "plan_wikivoyage_trip") {
      setStage("weather", "done", "最佳日期已确定");
      setStage("guide", "active", "正在提取景点与攻略");
      setStage("route", "active", "准备交通矩阵");
    }
  }

  function handleToolCompleted(name, succeeded) {
    if (name === "find_best_weather_window") {
      setStage("weather", succeeded ? "done" : "active", succeeded ? "天气候选已生成" : "天气服务降级");
      if (!succeeded) showError("天气 Tool 执行失败。");
    } else if (name === "plan_wikivoyage_trip") {
      setStage("guide", succeeded ? "done" : "active", succeeded ? "攻略与景点已提取" : "攻略服务降级");
      setStage("route", succeeded ? "done" : "active", succeeded ? "每日路线已优化" : "路线服务降级");
      setStage("complete", "active", "Agent 正在组织回答");
      if (!succeeded) showError("攻略 Tool 执行失败。");
    }
    fetchFinalRun({ refreshMessages: false });
  }

  function renderWeather(result) {
    state.weather = result;
    const location = result.resolved_location || {};
    const locationParts = [location.name, location.admin1, location.country].filter(Boolean);
    $("#weather-location").textContent = locationParts.join(" · ") || result.query_city || "天气位置已解析";
    if (result.notice) addWarning(result.notice);
    if ((result.skipped_dates || []).length) {
      addWarning("有 " + result.skipped_dates.length + " 个预报日期因数据不完整被跳过。");
    }

    const dailyLookup = new Map(
      (result.all_daily_weather || []).map((day) => [day.date, day])
    );
    const windows = (result.top_windows || []).slice(0, 3);
    const best = result.best_window || {};
    const container = $("#weather-candidates");
    container.replaceChildren();
    windows.forEach((windowItem, index) => {
      const isBest =
        windowItem.start_date === best.start_date &&
        windowItem.end_date === best.end_date;
      const card = create("article", "weather-card" + (isBest ? " is-best" : ""));
      const top = create("div", "weather-card-top");
      top.append(
        create("span", "weather-rank", isBest ? "BEST WINDOW" : "OPTION " + String(index + 1).padStart(2, "0")),
        create("span", "weather-badge", isBest ? "推荐" : "备选")
      );
      const score = create("p", "weather-score", Number(windowItem.average_score || 0).toFixed(1));
      score.append(create("small", "", "/ 100"));
      const dates = create(
        "p",
        "weather-dates",
        formatDate(windowItem.start_date, shortDateFormatter) + " — " +
          formatDate(windowItem.end_date, shortDateFormatter)
      );
      const days = create("div", "weather-days");
      (windowItem.dates || []).forEach((date) => {
        const weather = dailyLookup.get(date) || {};
        const day = create("div", "weather-day");
        day.append(
          create("span", "", formatDate(date)),
          create(
            "strong",
            "",
            (weather.weather || "—") + " · " +
              (weather.temp_max_c !== undefined ? weather.temp_max_c + "°" : "—")
          )
        );
        days.append(day);
      });
      card.append(top, score, dates, days);
      container.append(card);
    });
    if (!windows.length) {
      container.append(create("div", "empty-copy", "天气 Tool 未返回可展示的候选窗口。"));
    }
    renderSources();
  }

  function renderPlan(result) {
    state.plan = result;
    state.currentDay = 0;
    (result.warnings || []).forEach(addWarning);
    state.extraSources = result.source_pages || [];
    renderSources();
    renderDayTabs();
    renderItinerary();
    renderMapDay(0);
  }

  function renderDayTabs() {
    const tabs = $("#day-tabs");
    tabs.replaceChildren();
    (state.plan.itinerary || []).forEach((day, index) => {
      const button = create("button", "day-tab" + (index === 0 ? " is-active" : ""), "DAY " + (index + 1));
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", index === 0 ? "true" : "false");
      button.addEventListener("click", () => {
        state.currentDay = index;
        $$(".day-tab").forEach((tab, tabIndex) => {
          tab.classList.toggle("is-active", tabIndex === index);
          tab.setAttribute("aria-selected", tabIndex === index ? "true" : "false");
        });
        renderMapDay(index);
      });
      tabs.append(button);
    });
  }

  function renderItinerary() {
    const container = $("#itinerary-days");
    const itinerary = state.plan.itinerary || [];
    container.replaceChildren();
    $("#itinerary-summary").textContent =
      itinerary.length + " 天 · " +
      itinerary.reduce((sum, day) => sum + Number(day.attraction_count || 0), 0) +
      " 个景点";

    itinerary.forEach((day, index) => {
      const article = create("article", "itinerary-day");
      const meta = create("div", "day-meta");
      meta.append(
        create("span", "", "DAY " + String(index + 1).padStart(2, "0")),
        create("strong", "", formatDate(day.date)),
        create(
          "small",
          "",
          ((day.weather || {}).weather || "天气未知") + " · " +
            (day.distance_km || 0) + " km · " +
            (day.travel_minutes || 0) + " min 交通"
        )
      );
      const timeline = create("div", "timeline");
      (day.timeline || [])
        .filter((item) => item.type === "attraction" || item.type === "meal_break")
        .forEach((item) => {
          const stop = create("div", "timeline-stop");
          const start = item.start || "—";
          const end = item.end || "";
          stop.append(
            create("time", "", end ? start + "—" + end : start),
            create("strong", "", item.name || "未命名地点"),
            create(
              "span",
              "",
              item.type === "meal_break"
                ? "用餐与休息"
                : (item.visit_minutes || 0) + " 分钟游览"
            )
          );
          timeline.append(stop);
        });
      article.append(meta, timeline);
      container.append(article);
    });
    if (!itinerary.length) {
      container.append(create("div", "empty-copy", "攻略 Tool 未返回逐日行程。"));
    }
  }

  function routePoints(day) {
    const attractionMap = new Map(
      (day.attractions || []).map((attraction) => [attraction.name, attraction])
    );
    const orderedNames = (day.timeline || [])
      .filter((item) => item.type === "attraction")
      .map((item) => item.name);
    const attractions = orderedNames.length
      ? orderedNames.map((name) => attractionMap.get(name)).filter(Boolean)
      : (day.attractions || []);
    return attractions
      .map((attraction) => {
        const coordinates = attraction.coordinates || {};
        const latitude = Number(coordinates.latitude);
        const longitude = Number(coordinates.longitude);
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
        return {
          name: attraction.name || "未命名景点",
          latitude,
          longitude,
          description: attraction.description || "",
          visitMinutes: attraction.visit_minutes || 0,
        };
      })
      .filter(Boolean);
  }

  function clearMap() {
    $("#map").classList.add("is-hidden");
    $("#map-fallback").classList.add("is-hidden");
    $("#map-empty").classList.remove("is-hidden");
    if (state.mapLayer) state.mapLayer.clearLayers();
  }

  function initLeafletMap() {
    if (state.map || !window.L) return Boolean(state.map);
    state.map = window.L.map("map", {
      zoomControl: true,
      scrollWheelZoom: false,
      attributionControl: true,
    });
    state.tileLayer = window.L.tileLayer(
      "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors",
      }
    );
    let tileWarningShown = false;
    state.tileLayer.on("tileerror", () => {
      if (!tileWarningShown) {
        tileWarningShown = true;
        addWarning("OpenStreetMap 瓦片加载失败；景点坐标与路线标记仍可查看。");
      }
    });
    state.tileLayer.addTo(state.map);
    state.mapLayer = window.L.layerGroup().addTo(state.map);
    return true;
  }

  function renderMapDay(index) {
    const itinerary = (state.plan && state.plan.itinerary) || [];
    const day = itinerary[index];
    if (!day) {
      clearMap();
      return;
    }
    $("#map-title").textContent = "Day " + (index + 1) + " · " + formatDate(day.date);
    const summary = $("#route-summary");
    summary.textContent =
      (day.route_summary || "路线顺序待确认") +
      " · " + (day.distance_km || 0) + " km · 预计 " +
      (day.estimated_end || "—") + " 结束";
    summary.classList.add("is-visible");
    const points = routePoints(day);
    if (!points.length) {
      clearMap();
      $("#map-empty strong").textContent = "本日景点缺少可用坐标";
      $("#map-empty span").textContent = "请查看下方文字行程，并在出发前复核地点";
      return;
    }
    $("#map-empty").classList.add("is-hidden");
    if (initLeafletMap()) renderLeaflet(points);
    else renderFallbackMap(points);
  }

  function renderLeaflet(points) {
    $("#map-fallback").classList.add("is-hidden");
    $("#map").classList.remove("is-hidden");
    state.mapLayer.clearLayers();
    const latLngs = points.map((point) => [point.latitude, point.longitude]);
    window.L.polyline(latLngs, {
      color: "#164f42",
      weight: 4,
      opacity: 0.82,
      dashArray: "2 9",
    }).addTo(state.mapLayer);
    points.forEach((point, index) => {
      const markerNode = create("div", "route-marker");
      markerNode.append(create("span", "", index + 1));
      const icon = window.L.divIcon({
        className: "",
        html: markerNode.outerHTML,
        iconSize: [28, 28],
        iconAnchor: [14, 27],
      });
      const popup = create("div");
      popup.append(
        create("strong", "", String(index + 1).padStart(2, "0") + " · " + point.name),
        create("p", "", (point.visitMinutes || 0) + " 分钟 · " + point.description.slice(0, 90))
      );
      window.L.marker([point.latitude, point.longitude], { icon })
        .bindPopup(popup)
        .addTo(state.mapLayer);
    });
    state.map.fitBounds(window.L.latLngBounds(latLngs), {
      padding: [42, 42],
      maxZoom: 14,
    });
    window.setTimeout(() => state.map.invalidateSize(), 80);
  }

  function renderFallbackMap(points) {
    $("#map").classList.add("is-hidden");
    const svg = $("#map-fallback");
    svg.classList.remove("is-hidden");
    svg.replaceChildren();
    const namespace = "http://www.w3.org/2000/svg";
    const lats = points.map((point) => point.latitude);
    const lngs = points.map((point) => point.longitude);
    const minLat = Math.min(...lats);
    const maxLat = Math.max(...lats);
    const minLng = Math.min(...lngs);
    const maxLng = Math.max(...lngs);
    const xRange = maxLng - minLng || 1;
    const yRange = maxLat - minLat || 1;
    const projected = points.map((point) => ({
      ...point,
      x: 90 + ((point.longitude - minLng) / xRange) * 620,
      y: 355 - ((point.latitude - minLat) / yRange) * 280,
    }));
    const path = document.createElementNS(namespace, "polyline");
    path.setAttribute("points", projected.map((point) => point.x + "," + point.y).join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#164f42");
    path.setAttribute("stroke-width", "4");
    path.setAttribute("stroke-dasharray", "4 10");
    svg.append(path);
    projected.forEach((point, index) => {
      const circle = document.createElementNS(namespace, "circle");
      circle.setAttribute("cx", point.x);
      circle.setAttribute("cy", point.y);
      circle.setAttribute("r", "15");
      circle.setAttribute("fill", "#164f42");
      svg.append(circle);
      const number = document.createElementNS(namespace, "text");
      number.setAttribute("x", point.x);
      number.setAttribute("y", point.y + 4);
      number.setAttribute("text-anchor", "middle");
      number.setAttribute("fill", "#fff");
      number.setAttribute("font-size", "11");
      number.textContent = String(index + 1);
      svg.append(number);
      const label = document.createElementNS(namespace, "text");
      label.setAttribute("x", point.x);
      label.setAttribute("y", point.y - 24);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("fill", "#34483f");
      label.setAttribute("font-size", "12");
      label.textContent = point.name.slice(0, 12);
      svg.append(label);
    });
  }

  function renderSources(finalReferences) {
    const container = $("#source-list");
    container.replaceChildren();
    const addSource = (kind, title, status, url) => {
      const row = create("div", "source-item");
      row.append(create("span", "", kind));
      const strong = create("strong");
      if (url) {
        const link = create("a", "", title);
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer";
        strong.append(link);
      } else {
        strong.textContent = title;
      }
      row.append(strong, create("em", status === "已使用" ? "is-ready" : "", status));
      container.append(row);
    };
    addSource(
      "天气",
      "Open-Meteo",
      state.weather ? "已使用" : "等待中",
      state.weather && state.weather.source && state.weather.source.forecast_url
    );
    addSource(
      "路线",
      "OpenRouteService",
      state.plan ? "已使用" : "等待中",
      "https://openrouteservice.org/"
    );
    const references = [
      ...state.extraSources,
      ...(finalReferences || []),
    ];
    const seen = new Set();
    references.forEach((source) => {
      const key = source.url || source.title;
      if (!key || seen.has(key) || seen.size >= 8) return;
      seen.add(key);
      addSource("攻略", source.title || "Wikivoyage", "已引用", source.url);
    });
    if (!references.length) {
      addSource("攻略", "Wikivoyage", state.plan ? "无页面" : "等待中");
    }
  }

  function renderContextUsage(usage) {
    if (!usage) return;
    const used = Number(usage.estimated_input_tokens || 0).toLocaleString("zh-CN");
    const limit = Number(usage.max_input_tokens || 0).toLocaleString("zh-CN");
    $("#context-tokens").textContent = used + " / " + limit + " tokens";
    $("#context-history").textContent = "历史 " + Number(usage.history_messages_used || 0) + " 条";
    $("#context-summary").textContent = usage.summary_present
      ? (usage.summary_updated ? "摘要已更新" : "摘要已复用")
      : "摘要未使用";
  }

  function renderRunOutput(output) {
    if (!output) return;
    state.warnings = new Set(defaultWarnings);
    (output.warnings || []).forEach((warning) => {
      addWarning(typeof warning === "string" ? warning : warning.message);
    });
    if (output.weather_window) renderWeather(output.weather_window);
    if (output.itinerary) renderPlan(output.itinerary);
    renderContextUsage(output.context_usage);
    renderSources(output.sources || []);

    const inherited = Object.values(output.components || {}).filter(
      (component) => component && component.inherited_from_run_id
    ).length;
    if (inherited) {
      addWarning("当前版本有 " + inherited + " 个组件沿用上一版结果；未重复调用对应 Tool。");
    }
    if (output.assistant_answer) {
      $("#answer-panel").classList.remove("is-hidden");
      $("#final-answer").textContent = output.assistant_answer;
    } else {
      $("#answer-panel").classList.add("is-hidden");
      $("#final-answer").textContent = "";
    }
  }

  async function fetchFinalRun({ refreshMessages = true } = {}) {
    if (!state.runId) return;
    try {
      const result = await requestJson("/v1/runs/" + state.runId, { method: "GET" });
      const run = result.body;
      if (run.error_message) showError(run.error_message);
      renderRunOutput(run.output);
      if (refreshMessages && state.sessionId) await loadMessageTimeline(state.sessionId);
    } catch (error) {
      addWarning("最终 Run 查询失败：" + error.message);
    }
  }

  async function refreshCsrfToken() {
    const response = await fetch("/v1/auth/csrf", {
      method: "GET",
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("无法初始化安全会话，请刷新页面。");
    const body = await response.json();
    state.csrfToken = body.csrf_token;
  }

  function renderAuthState() {
    const label = $("#account-label");
    if (state.user) {
      label.textContent = state.user.email;
      $("#auth-close").classList.remove("is-hidden");
      $("#history-button").classList.remove("is-hidden");
    } else {
      label.textContent = "登录";
      $("#auth-close").classList.add("is-hidden");
      $("#history-button").classList.add("is-hidden");
    }
    updateComposerState();
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    const registering = mode === "register";
    $("#auth-title").textContent = registering ? "创建 Atlas 账号" : "登录 Atlas";
    $("#auth-submit").firstElementChild.textContent = registering ? "创建账号并继续" : "登录并继续";
    $("#auth-mode").textContent = registering
      ? "已有账号？返回登录"
      : "还没有账号？创建账号";
    $("#auth-password").autocomplete = registering ? "new-password" : "current-password";
    $("#auth-error").classList.add("is-hidden");
  }

  function openAuthDialog() {
    setAuthMode("login");
    const dialog = $("#auth-dialog");
    if (dialog.open) return;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
      dialog.classList.add("dialog-fallback");
    }
  }

  function closeAuthDialog() {
    const dialog = $("#auth-dialog");
    if (typeof dialog.close === "function") {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
      dialog.classList.remove("dialog-fallback");
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    const errorNode = $("#auth-error");
    errorNode.classList.add("is-hidden");
    const submit = $("#auth-submit");
    submit.disabled = true;
    try {
      if (!state.csrfToken) await refreshCsrfToken();
      const formData = new FormData(event.currentTarget);
      const result = await requestJson("/v1/auth/" + state.authMode, {
        method: "POST",
        body: JSON.stringify({
          email: String(formData.get("email") || "").trim(),
          password: String(formData.get("password") || ""),
        }),
      });
      state.user = result.body;
      renderAuthState();
      closeAuthDialog();
      event.currentTarget.reset();
      await loadSessionHistory({ restoreLatest: true });
    } catch (error) {
      errorNode.textContent = error.message;
      errorNode.classList.remove("is-hidden");
    } finally {
      submit.disabled = false;
    }
  }

  async function logout() {
    try {
      await requestJson("/v1/auth/logout", { method: "POST" });
    } finally {
      if (state.eventSource) state.eventSource.close();
      state.eventSource = null;
      state.user = null;
      state.sessionId = null;
      state.runId = null;
      state.runActive = false;
      state.initialSessionRestored = false;
      state.sessionItems = [];
      renderWorkspaceSessions([]);
      renderConversation([]);
      $("#workspace").classList.add("is-idle");
      renderAuthState();
      try {
        await refreshCsrfToken();
      } catch (_) {
        state.csrfToken = null;
      }
      openAuthDialog();
    }
  }

  async function bootstrapAuth() {
    try {
      await refreshCsrfToken();
      const result = await requestJson("/v1/auth/me", { method: "GET" });
      state.user = result.body;
      renderAuthState();
      await loadSessionHistory({ restoreLatest: true });
    } catch (_) {
      state.user = null;
      renderAuthState();
      openAuthDialog();
    }
  }

  function openHistoryDialog() {
    const dialog = $("#history-dialog");
    if (dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeHistoryDialog() {
    const dialog = $("#history-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function formatSessionTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function statusLabel(status) {
    const labels = {
      PENDING: "排队中",
      RUNNING: "执行中",
      WAITING_FOR_USER: "等待补充",
      SUCCEEDED: "已完成",
      FAILED: "失败",
      CANCELLED: "已取消",
    };
    return labels[status] || "暂无 Run";
  }

  async function loadSessionHistory({ restoreLatest = false } = {}) {
    if (!state.user) return;
    const includeArchived = $("#history-archived").checked;
    try {
      const result = await requestJson(
        "/v1/sessions?page=" + state.historyPage +
        "&page_size=" + state.historyPageSize +
        "&include_archived=" + includeArchived,
        { method: "GET" }
      );
      const data = result.body;
      state.historyTotal = Number(data.total || 0);
      state.sessionItems = data.items || [];
      renderSessionHistory(data.items || []);
      renderWorkspaceSessions((data.items || []).filter((item) => item.status !== "ARCHIVED"));
      $("#history-summary").textContent = "共 " + state.historyTotal + " 个会话";
      $("#history-page").textContent = "第 " + state.historyPage + " 页";
      $("#history-previous").disabled = state.historyPage <= 1;
      $("#history-next").disabled =
        state.historyPage * state.historyPageSize >= state.historyTotal;
      if (
        restoreLatest &&
        !state.initialSessionRestored &&
        data.items &&
        data.items.length
      ) {
        state.initialSessionRestored = true;
        await restoreSession(data.items[0], { closeDialog: false });
      }
    } catch (error) {
      $("#history-summary").textContent = "历史会话读取失败：" + error.message;
    }
  }

  function renderSessionHistory(items) {
    const container = $("#history-list");
    container.replaceChildren();
    if (!items.length) {
      container.append(create("div", "empty-copy", "还没有历史会话。"));
      return;
    }
    items.forEach((item) => {
      const card = create(
        "article",
        "history-item" + (item.id === state.sessionId ? " is-current" : "")
      );
      card.tabIndex = 0;
      const top = create("div", "history-item-top");
      top.append(
        create("h3", "", item.title || "未命名旅行"),
        create("span", "history-run-status", statusLabel(item.last_run_status))
      );
      const preview = create(
        "p",
        "",
        item.recent_message_preview || "尚未发送消息"
      );
      const meta = create("div", "history-item-meta");
      meta.append(
        create("span", "", formatSessionTime(item.updated_at)),
        create("span", "", Number(item.message_count || 0) + " 条消息"),
        create("span", "", item.status === "ARCHIVED" ? "已归档" : "进行中")
      );
      const actions = create("div", "history-actions");
      const rename = create("button", "", "重命名");
      const archive = create(
        "button",
        "",
        item.status === "ARCHIVED" ? "取消归档" : "归档"
      );
      const remove = create("button", "", "删除");
      [rename, archive, remove].forEach((button) => {
        button.type = "button";
        button.addEventListener("click", (event) => event.stopPropagation());
      });
      rename.addEventListener("click", () => renameSession(item));
      archive.addEventListener("click", () => archiveSession(item));
      remove.addEventListener("click", () => deleteSession(item));
      actions.append(rename, archive, remove);
      card.append(top, preview, meta, actions);
      card.addEventListener("click", () => restoreSession(item));
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter") restoreSession(item);
      });
      container.append(card);
    });
  }

  function renderWorkspaceSessions(items) {
    const container = $("#workspace-session-list");
    container.replaceChildren();
    if (!items.length) {
      container.append(create("p", "rail-empty", "还没有旅行会话。创建一次规划后，可以在这里随时继续讨论。"));
      return;
    }
    items.slice(0, 8).forEach((item) => {
      const button = create(
        "button",
        "workspace-session-item" + (item.id === state.sessionId ? " is-current" : "")
      );
      button.type = "button";
      button.append(
        create("strong", "", item.title || "未命名旅行"),
        create(
          "span",
          "",
          statusLabel(item.last_run_status) + " · " + Number(item.message_count || 0) + " 条消息"
        )
      );
      button.addEventListener("click", () => restoreSession(item, { closeDialog: false }));
      container.append(button);
    });
  }

  async function loadMessageTimeline(sessionId) {
    const timeline = $("#history-timeline");
    try {
      const result = await requestJson(
        "/v1/sessions/" + sessionId + "/messages",
        { method: "GET" }
      );
      renderConversation(result.body || []);
      timeline.replaceChildren();
      (result.body || []).slice(-12).forEach((message) => {
        const node = create(
          "div",
          "history-message" + (message.role === "ASSISTANT" ? " is-assistant" : "")
        );
        node.append(
          create("strong", "", message.role === "ASSISTANT" ? "AGENT" : "YOU"),
          create("span", "", message.content)
        );
        timeline.append(node);
      });
      timeline.classList.toggle("is-hidden", !result.body.length);
    } catch (_) {
      timeline.classList.add("is-hidden");
    }
  }

  async function restoreSession(item, { closeDialog = true } = {}) {
    resetWorkspace(item.title || "历史旅行");
    state.sessionId = item.id;
    renderWorkspaceSessions(state.sessionItems.filter((session) => session.status !== "ARCHIVED"));
    updateComposerState();
    $("#journey-title").textContent = (item.title || "历史旅行") + " · 历史会话";
    await loadMessageTimeline(item.id);
    if (closeDialog) closeHistoryDialog();
    if (!item.last_run_id) {
      setStatus("尚未规划", "idle");
      setButtonRunning(false);
      return;
    }
    state.runId = item.last_run_id;
    $("#run-reference").textContent =
      "RUN " + state.runId.slice(0, 8).toUpperCase();
    setStatus(statusLabel(item.last_run_status), "running");
    setButtonRunning(["PENDING", "RUNNING"].includes(item.last_run_status));
    await fetchFinalRun({ refreshMessages: false });
    openEventStream("/v1/runs/" + state.runId + "/events");
  }

  async function renameSession(item) {
    const title = window.prompt("新的会话名称", item.title || "");
    if (!title || !title.trim()) return;
    await requestJson("/v1/sessions/" + item.id, {
      method: "PATCH",
      body: JSON.stringify({ title: title.trim() }),
    });
    if (state.sessionId === item.id) {
      $("#journey-title").textContent = title.trim() + " · 历史会话";
    }
    await loadSessionHistory();
  }

  async function archiveSession(item) {
    await requestJson("/v1/sessions/" + item.id, {
      method: "PATCH",
      body: JSON.stringify({ archived: item.status !== "ARCHIVED" }),
    });
    await loadSessionHistory();
  }

  async function deleteSession(item) {
    if (!window.confirm("确定永久删除“" + (item.title || "未命名旅行") + "”及其消息和 Run 吗？")) {
      return;
    }
    await requestJson("/v1/sessions/" + item.id, { method: "DELETE" });
    if (state.sessionId === item.id) {
      if (state.eventSource) state.eventSource.close();
      state.sessionId = null;
      state.runId = null;
      $("#workspace").classList.add("is-idle");
    }
    await loadSessionHistory();
  }

  $("#planner-form").addEventListener("submit", submitJourney);
  $("#follow-up-form").addEventListener("submit", submitFollowUp);
  $("#follow-up-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $("#follow-up-form").requestSubmit();
    }
  });
  $$("[data-follow-up]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $("#follow-up-input");
      input.value = button.dataset.followUp || "";
      input.focus();
    });
  });
  $("#new-journey-button").addEventListener("click", () => {
    $("#planner-form").scrollIntoView({ behavior: "smooth", block: "center" });
    $("#city").focus();
  });
  $("#manage-sessions-button").addEventListener("click", async () => {
    if (!state.user) {
      openAuthDialog();
      return;
    }
    await loadSessionHistory();
    openHistoryDialog();
  });
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#auth-mode").addEventListener("click", () => {
    setAuthMode(state.authMode === "login" ? "register" : "login");
  });
  $("#auth-close").addEventListener("click", closeAuthDialog);
  $("#auth-dialog").addEventListener("cancel", (event) => {
    if (!state.user) event.preventDefault();
  });
  $("#account-button").addEventListener("click", () => {
    if (state.user) {
      logout();
    } else {
      openAuthDialog();
    }
  });
  $("#history-button").addEventListener("click", async () => {
    await loadSessionHistory();
    openHistoryDialog();
  });
  $("#history-close").addEventListener("click", closeHistoryDialog);
  $("#history-archived").addEventListener("change", () => {
    state.historyPage = 1;
    loadSessionHistory();
  });
  $("#history-previous").addEventListener("click", () => {
    if (state.historyPage <= 1) return;
    state.historyPage -= 1;
    loadSessionHistory();
  });
  $("#history-next").addEventListener("click", () => {
    if (state.historyPage * state.historyPageSize >= state.historyTotal) return;
    state.historyPage += 1;
    loadSessionHistory();
  });
  renderWarnings();
  renderSources();
  updateComposerState();
  bootstrapAuth();
})();
