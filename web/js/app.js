/**
 * 面试复盘助手 — 本地前端（SSE 进度 + 逐题/长篇/JD）
 */

(() => {
  const API = "/api/v1";

  const els = {
    sidebar: document.getElementById("sidebar"),
    sessionList: document.getElementById("session-list"),
    workspace: document.getElementById("workspace"),
    topbarTitle: document.getElementById("topbar-title"),
    topbarActions: document.getElementById("topbar-actions"),
    btnNew: document.getElementById("btn-new"),
    btnToggle: document.getElementById("btn-toggle-sidebar"),
    toastHost: document.getElementById("toast-host"),
  };

  const state = {
    sessions: [],
    activeId: null,
    detail: null,
    tab: "per_qa", // per_qa | report | jd
    qaIndex: 0,
    generating: false,
    live: null, // { sessionId, qaItems, analyses, completedCount, totalCount, reviewMd, jd, position, title, paused, done }
    jdMode: "text", // text | file
    selectedVersions: {}, // { qa_id: versionNumber } 刷新后默认最新
    revising: false,
    // 阶段读秒（需求4）
    stageBaseLabel: "",
    stageStartedAt: 0,
    stageTimerId: null,
    stageStep: null,
  };

  function groupAnalysesByQa(analyses) {
    const map = {};
    (analyses || []).forEach((a) => {
      const id = a.qa_id;
      if (!id) return;
      if (!map[id]) map[id] = [];
      map[id].push(a);
    });
    Object.keys(map).forEach((id) => {
      map[id].sort((x, y) => (Number(x.version) || 1) - (Number(y.version) || 1));
    });
    return map;
  }

  function uniqueCompletedCount(analyses) {
    return new Set((analyses || []).map((a) => a.qa_id).filter(Boolean)).size;
  }

  function pickAnalysisVersion(qaId, versions) {
    if (!versions || !versions.length) return null;
    const want = state.selectedVersions[qaId];
    if (want != null) {
      const found = versions.find((v) => (Number(v.version) || 1) === Number(want));
      if (found) return found;
    }
    return versions[versions.length - 1];
  }

  function canReviseQa(vm) {
    return vm.status === "completed" || vm.status === "paused" || !!vm.paused;
  }

  function stopStageTimer() {
    if (state.stageTimerId) {
      clearInterval(state.stageTimerId);
      state.stageTimerId = null;
    }
  }

  function paintStageLabel() {
    const lab = document.getElementById("progress-label");
    if (!lab || !state.stageBaseLabel) return;
    const sec = Math.max(0, Math.floor((Date.now() - state.stageStartedAt) / 1000));
    lab.textContent = `${state.stageBaseLabel}...${sec}s`;
  }

  /** 开始新阶段并重置读秒；阶段结束时请 stopStageTimer() */
  function startStageTimer(baseLabel, stepKey) {
    state.stageBaseLabel = baseLabel || "";
    state.stageStartedAt = Date.now();
    if (stepKey !== undefined) state.stageStep = stepKey;
    stopStageTimer();
    paintStageLabel();
    state.stageTimerId = setInterval(paintStageLabel, 1000);
  }

  /** 同阶段内只改文案，不重置秒数 */
  function updateStageLabel(baseLabel) {
    state.stageBaseLabel = baseLabel || state.stageBaseLabel;
    if (!state.stageTimerId) {
      stopStageTimer();
      state.stageTimerId = setInterval(paintStageLabel, 1000);
    }
    paintStageLabel();
  }

  function toast(msg, type = "") {
    const el = document.createElement("div");
    el.className = `toast ${type}`.trim();
    el.textContent = msg;
    els.toastHost.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  async function api(path, options = {}) {
    const res = await fetch(`${API}${path}`, options);
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) data = await res.json();
    else data = await res.text();
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : String(data);
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderMarkdown(md) {
    if (typeof marked === "undefined") {
      return `<pre class="raw-box">${escapeHtml(md || "")}</pre>`;
    }
    marked.setOptions({ breaks: true, gfm: true });
    return marked.parse(md || "_（空）_");
  }

  function formatTime(iso) {
    if (!iso) return "";
    return iso.replace("T", " ").slice(0, 16);
  }

  function statusLabel(s) {
    return (
      {
        pending: "待处理",
        running: "生成中",
        completed: "已完成",
        failed: "失败",
        awaiting_transcript: "待确认转写",
        awaiting_questions: "待确认问题",
        awaiting_qa: "待确认问答",
        analyzing: "逐题分析中",
        paused: "已暂停",
      }[s] || s
    );
  }

  function closeSidebarOnMobile() {
    els.sidebar.classList.remove("open");
  }

  function analysisToMarkdown(a, qa) {
    if (!a) return "_尚未生成_";
    if (a.raw_markdown && (a.assessment_point || a.improved_answer || (a.issues && a.issues.length))) {
      // prefer structured
    }
    if (a.assessment_point || a.improved_answer || (a.issues && a.issues.length)) {
      const issues = (a.issues || []).map((i) => `- ${i}`).join("\n") || "_（无）_";
      return [
        `## ${qa?.id || a.qa_id || ""}｜${qa?.topic || ""}`,
        "",
        `**问：** ${qa?.question || ""}`,
        "",
        "### 1. 问题考察点",
        "",
        a.assessment_point || "_（空）_",
        "",
        "### 2. 原文回答",
        "",
        a.original_answer || qa?.answer || "_（空）_",
        "",
        "### 3. 原回答不妥之处",
        "",
        issues,
        "",
        "### 4. 合理回答（改进示例）",
        "",
        a.improved_answer || "_（空）_",
      ].join("\n");
    }
    return a.raw_markdown || qa?.answer || "_（空）_";
  }

  // ---------- sessions list ----------

  async function loadSessions() {
    const data = await api("/sessions?limit=100");
    state.sessions = data.sessions || [];
    renderSessionList();
  }

  function renderSessionList() {
    const list = els.sessionList;
    if (!state.sessions.length) {
      list.innerHTML = `<div class="session-empty">还没有历史会话。<br />点上方「新建复盘」开始。</div>`;
      return;
    }
    list.innerHTML = state.sessions
      .map((s) => {
        const active = s.session_id === state.activeId ? "active" : "";
        const sid = escapeHtml(s.session_id);
        const title = escapeHtml(s.title || s.session_id);
        return `
          <div class="session-item ${active}" data-id="${sid}" role="listitem">
            <button type="button" class="session-item-main" data-id="${sid}" title="${title}">
              <div class="t">${title}</div>
              <div class="m">
                <span>${escapeHtml(statusLabel(s.status))}</span>
                <span>${s.qa_count || 0} 题</span>
                <span>${escapeHtml(formatTime(s.updated_at))}</span>
              </div>
            </button>
            <div class="session-item-actions">
              <button type="button" class="session-act session-act-rename" data-id="${sid}" title="重命名" aria-label="重命名">
                <span class="session-act-r" aria-hidden="true">R</span>
              </button>
              <button type="button" class="session-act session-act-delete" data-id="${sid}" title="删除" aria-label="删除">
                <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 6h18"/>
                  <path d="M8 6V4h8v2"/>
                  <path d="M19 6l-1 14H6L5 6"/>
                  <path d="M10 11v6"/>
                  <path d="M14 11v6"/>
                </svg>
              </button>
            </div>
          </div>`;
      })
      .join("");
    list.querySelectorAll(".session-item-main").forEach((btn) => {
      btn.addEventListener("click", () => openSession(btn.dataset.id));
    });
    list.querySelectorAll(".session-act-rename").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = btn.dataset.id;
        const meta = state.sessions.find((x) => x.session_id === id);
        renameSession(id, meta?.title || "");
      });
    });
    list.querySelectorAll(".session-act-delete").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        deleteSession(btn.dataset.id);
      });
    });
  }

  function stopWatchProgress() {
    if (state.watchTimer) {
      clearInterval(state.watchTimer);
      state.watchTimer = null;
    }
    state.watchSessionId = null;
    state.watchFingerprint = null;
  }

  function sessionWatchFingerprint(detail) {
    const m = detail?.meta || {};
    const n = (detail?.analyses || []).length;
    return `${m.status}|${m.workflow_step}|${n}|${m.updated_at || ""}`;
  }

  function paintSessionDetail(id, detail) {
    state.detail = detail;
    const step = detail.meta?.workflow_step || "";
    const st = detail.meta?.status || "";
    if (
      st === "awaiting_transcript" ||
      (step === "transcript" && detail.cleaned_transcript)
    ) {
      renderWizardStep("transcript", {
        session_id: id,
        cleaned_transcript: detail.cleaned_transcript,
        message: "请核对转写文稿，可编辑后点击下一步",
      });
    } else if (
      st === "awaiting_questions" ||
      (step === "questions" && detail.clarified_questions)
    ) {
      renderWizardStep("questions", {
        session_id: id,
        clarified_questions: detail.clarified_questions,
        message: "请核对澄清后的问题列表，可编辑后点击下一步",
      });
    } else if (st === "awaiting_qa" || (step === "qa" && detail.interview_qa_md)) {
      renderWizardStep("qa", {
        session_id: id,
        interview_qa_md: detail.interview_qa_md,
        message: "请核对问答对，可编辑后开始逐题分析",
      });
    } else {
      renderResultView();
    }
  }

  function startWatchProgress(sessionId) {
    /** 外部脚本驱动流程时：打开会话后轮询，刷新向导/分析进度。 */
    if (!sessionId) return;
    if (state.watchSessionId === sessionId && state.watchTimer) return;
    stopWatchProgress();
    state.watchSessionId = sessionId;
    state.watchTimer = setInterval(async () => {
      if (state.activeId !== sessionId) {
        stopWatchProgress();
        return;
      }
      if (state.live && !state.live.done) return;
      try {
        const detail = await api(`/sessions/${encodeURIComponent(sessionId)}`);
        const fp = sessionWatchFingerprint(detail);
        const st = detail.meta?.status || "";
        await loadSessions();
        if (fp !== state.watchFingerprint) {
          state.watchFingerprint = fp;
          state.live = null;
          paintSessionDetail(sessionId, detail);
        }
        const inFlight =
          st === "awaiting_transcript" ||
          st === "awaiting_questions" ||
          st === "awaiting_qa" ||
          st === "analyzing" ||
          st === "running" ||
          st === "paused";
        if (!inFlight) stopWatchProgress();
      } catch (e) {
        console.warn("watch progress failed", e);
      }
    }, 2000);
  }

  async function openSession(id) {
    try {
      state.activeId = id;
      state.live = null;
      state.tab = "per_qa";
      state.qaIndex = 0;
      state.selectedVersions = {};
      renderSessionList();
      els.workspace.innerHTML = `<div class="panel empty-state">加载中…</div>`;
      const detail = await api(`/sessions/${encodeURIComponent(id)}`);
      state.watchFingerprint = sessionWatchFingerprint(detail);
      paintSessionDetail(id, detail);
      const st = detail.meta?.status || "";
      const inFlight =
        st === "awaiting_transcript" ||
        st === "awaiting_questions" ||
        st === "awaiting_qa" ||
        st === "analyzing" ||
        st === "running" ||
        st === "paused";
      if (inFlight) startWatchProgress(id);
      else stopWatchProgress();
      closeSidebarOnMobile();
    } catch (e) {
      toast(e.message || "加载失败", "error");
    }
  }

  // ---------- result view ----------

  function getViewModel() {
    if (state.live) {
      const paused = !!state.live.paused;
      return {
        sessionId: state.live.sessionId,
        title: state.live.title,
        position: state.live.position,
        status: state.live.done ? "completed" : paused ? "paused" : "analyzing",
        qaItems: state.live.qaItems,
        analyses: state.live.analyses,
        completedCount: state.live.completedCount,
        totalCount: state.live.totalCount,
        reviewMd: state.live.reviewMd,
        jd: state.live.jd,
        warnings: state.live.warnings || [],
        analyzing: !state.live.done,
        paused,
      };
    }
    const d = state.detail;
    if (!d) return null;
    const m = d.meta || {};
    const analyzing =
      m.status === "running" || m.status === "analyzing" || m.status === "paused";
    const analyses = d.analyses || [];
    return {
      sessionId: m.session_id,
      title: m.title,
      position: m.position || "待确认",
      status: m.status,
      qaItems: d.qa_items || [],
      analyses,
      completedCount: uniqueCompletedCount(analyses),
      totalCount: (d.qa_items || []).length,
      reviewMd: d.interview_review_md || "",
      jd: d.job_description || "",
      warnings: m.warnings || [],
      analyzing,
      paused: m.status === "paused",
    };
  }

  function renderResultView() {
    const vm = getViewModel();
    if (!vm) return;

    els.topbarTitle.textContent = vm.title || "会话详情";
    els.topbarActions.innerHTML = `
      <button type="button" class="btn-secondary" id="act-rename">重命名</button>
      <a class="btn-secondary" href="${API}/sessions/${encodeURIComponent(vm.sessionId)}/artifacts/interview_review.md" download="interview_review.md">下载报告</a>
      <button type="button" class="btn-danger" id="act-delete">删除</button>
    `;
    document.getElementById("act-rename").onclick = () => renameActive();
    document.getElementById("act-delete").onclick = () => deleteActive();

    const completed = vm.completedCount || 0;
    const maxIdx = Math.max(0, completed - 1);
    if (state.qaIndex > maxIdx) state.qaIndex = maxIdx;

    const warnHtml = (vm.warnings || []).length
      ? `<div class="warnings"><strong>提示</strong><ul>${vm.warnings
          .slice(0, 6)
          .map((w) => `<li>${escapeHtml(w)}</li>`)
          .join("")}</ul></div>`
      : "";

    let body = "";
    if (state.tab === "per_qa") {
      body = renderPerQaBody(vm);
    } else if (state.tab === "report") {
      body = `<div class="markdown-body">${renderMarkdown(vm.reviewMd || "_报告生成中或尚未完成…_")}</div>`;
    } else {
      body = vm.jd
        ? `<pre class="raw-box">${escapeHtml(vm.jd)}</pre>`
        : `<div class="empty-state">未上传岗位 JD</div>`;
    }

    const badge = vm.analyzing
      ? `<span class="analyzing-badge"><span class="spinner"></span>${
          vm.paused ? "已暂停" : "分析进行中"
        } ${completed}/${vm.totalCount || "?"} 题</span>`
      : "";

    const showAnalyzeControls = vm.analyzing && (state.live || vm.paused);
    const pauseControls = showAnalyzeControls
      ? `
      <div class="analyze-controls">
        <div class="progress-panel visible" id="progress-panel">
          <div id="progress-label"></div>
        </div>
        <button type="button" class="btn-secondary" id="btn-pause-analyze">
          ${vm.paused ? "继续分析" : "暂停分析"}
        </button>
      </div>`
      : "";

    els.workspace.innerHTML = `
      <div class="panel">
        <div class="meta-row">
          <span class="chip">${escapeHtml(vm.position || "待确认")}</span>
          <span>${escapeHtml(statusLabel(vm.status))}</span>
          ${badge}
        </div>
        ${warnHtml}
        <div class="tabs">
          <button type="button" class="tab ${state.tab === "per_qa" ? "active" : ""}" data-tab="per_qa">逐题分析</button>
          <button type="button" class="tab ${state.tab === "report" ? "active" : ""}" data-tab="report">长篇报告</button>
          <button type="button" class="tab ${state.tab === "jd" ? "active" : ""}" data-tab="jd">岗位JD查看</button>
        </div>
        <div id="tab-body">${body}</div>
        ${pauseControls}
      </div>
    `;

    // 结果页重绘后恢复读秒文案；历史「已暂停」无 live 时给静态提示
    if (vm.analyzing && state.stageBaseLabel) {
      paintStageLabel();
    } else if (vm.paused && !state.live) {
      const lab = document.getElementById("progress-label");
      if (lab) {
        lab.textContent = `已暂停（已完成 ${completed}/${vm.totalCount || "?"}），点击下方继续`;
      }
    }

    els.workspace.querySelectorAll(".tab").forEach((t) => {
      t.addEventListener("click", () => {
        state.tab = t.dataset.tab;
        renderResultView();
      });
    });

    if (state.tab === "per_qa") {
      const prev = document.getElementById("nav-prev");
      const next = document.getElementById("nav-next");
      if (prev) {
        prev.onclick = () => {
          if (state.qaIndex > 0) {
            state.qaIndex -= 1;
            renderResultView();
          }
        };
      }
      if (next) {
        next.onclick = () => {
          if (state.qaIndex < maxIdx) {
            state.qaIndex += 1;
            renderResultView();
          }
        };
      }
      const jumpInput = document.getElementById("qa-jump-input");
      if (jumpInput) {
        const maxCount = maxIdx + 1;
        const commit = () => jumpToQaIndex(jumpInput.value, maxCount);
        jumpInput.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          }
        });
        jumpInput.addEventListener("change", () => commit());
        jumpInput.addEventListener("focus", () => jumpInput.select());
      }
      bindPerQaVersionControls(vm);
    }

    const pauseBtn = document.getElementById("btn-pause-analyze");
    if (pauseBtn) {
      pauseBtn.onclick = () => togglePauseAnalyze(vm.sessionId);
    }
  }

  function currentPerQaContext(vm) {
    const byQa = groupAnalysesByQa(vm.analyses || []);
    const ordered = (vm.qaItems || []).filter((q) => byQa[q.id]?.length);
    const list =
      ordered.length > 0
        ? ordered
        : Object.keys(byQa).map((id) => ({ id, topic: "", question: "", answer: "" }));
    const idx = Math.min(state.qaIndex, Math.max(0, list.length - 1));
    const qa = list[idx];
    const qaId = qa?.id;
    const versions = byQa[qaId] || [];
    const analysis = pickAnalysisVersion(qaId, versions);
    return { qa, qaId, versions, analysis, idx };
  }

  function bindPerQaVersionControls(vm) {
    const ctx = currentPerQaContext(vm);
    document.querySelectorAll(".version-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const ver = Number(btn.dataset.version);
        if (!ctx.qaId || !Number.isFinite(ver)) return;
        state.selectedVersions[ctx.qaId] = ver;
        renderResultView();
      });
    });

    const delBtn = document.getElementById("btn-del-version");
    if (delBtn) {
      delBtn.onclick = () => deleteCurrentVersion(vm, ctx);
    }

    const reviseBtn = document.getElementById("btn-revise-qa");
    const modal = document.getElementById("revise-modal");
    const cancel = document.getElementById("revise-cancel");
    const submit = document.getElementById("revise-submit");
    if (reviseBtn && modal) {
      reviseBtn.onclick = () => {
        if (!canReviseQa(vm)) {
          toast("请在分析完成或暂停后再修改", "error");
          return;
        }
        modal.hidden = false;
        document.getElementById("revise-feedback")?.focus();
      };
    }
    if (cancel && modal) {
      cancel.onclick = () => {
        modal.hidden = true;
      };
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.hidden = true;
      });
    }
    if (submit) {
      submit.onclick = () => submitRevise(vm, ctx);
    }
  }

  async function deleteCurrentVersion(vm, ctx) {
    if (!ctx.qaId || !ctx.analysis) return;
    if ((ctx.versions || []).length <= 1) {
      toast("仅剩一个版本，不能删除", "error");
      return;
    }
    const ver = Number(ctx.analysis.version) || 1;
    if (!confirm(`确定删除 ${ctx.qaId} 的 V${ver}？`)) return;
    try {
      const data = await api(
        `/sessions/${encodeURIComponent(vm.sessionId)}/analyses/${encodeURIComponent(
          ctx.qaId
        )}/versions/${ver}`,
        { method: "DELETE" }
      );
      if (state.detail) {
        const others = (state.detail.analyses || []).filter(
          (a) => !(a.qa_id === ctx.qaId && (Number(a.version) || 1) === ver)
        );
        // 用接口返回的 versions 替换该题
        const kept = others.filter((a) => a.qa_id !== ctx.qaId);
        state.detail.analyses = kept.concat(data.versions || []);
        state.detail.interview_review_md = data.interview_review_md || state.detail.interview_review_md;
      }
      const latest = data.latest_version;
      if (latest != null) state.selectedVersions[ctx.qaId] = latest;
      else delete state.selectedVersions[ctx.qaId];
      toast("已删除该版本", "ok");
      renderResultView();
    } catch (e) {
      toast(e.message || "删除失败", "error");
    }
  }

  async function submitRevise(vm, ctx) {
    if (state.revising) return;
    const feedback = document.getElementById("revise-feedback")?.value?.trim() || "";
    if (!feedback) {
      toast("请填写修改要求", "error");
      return;
    }
    if (!ctx.qaId || !ctx.analysis) {
      toast("当前题目无效", "error");
      return;
    }
    const baseVersion = Number(ctx.analysis.version) || 1;
    const submit = document.getElementById("revise-submit");
    const cancel = document.getElementById("revise-cancel");
    const panel = document.getElementById("revise-progress");
    const lab = document.getElementById("revise-progress-label");
    state.revising = true;
    if (submit) submit.disabled = true;
    if (cancel) cancel.disabled = true;
    if (panel) panel.classList.add("visible");
    startStageTimer("正在按要求重新生成", "revise");
    if (lab) {
      // paintStageLabel 写 progress-label；修订弹窗用独立 id
      const tick = () => {
        const el = document.getElementById("revise-progress-label");
        if (!el || !state.stageBaseLabel) return;
        const sec = Math.max(0, Math.floor((Date.now() - state.stageStartedAt) / 1000));
        el.textContent = `${state.stageBaseLabel}...${sec}s`;
      };
      tick();
      // reuse interval via paint override: temporarily point paint to revise label
    }
    // 直接用 interval 刷修订进度文案
    const reviseTimer = setInterval(() => {
      const el = document.getElementById("revise-progress-label");
      if (!el) return;
      const sec = Math.max(0, Math.floor((Date.now() - state.stageStartedAt) / 1000));
      el.textContent = `正在按要求重新生成...${sec}s`;
    }, 1000);

    try {
      const data = await api(
        `/sessions/${encodeURIComponent(vm.sessionId)}/analyses/${encodeURIComponent(
          ctx.qaId
        )}/revise`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feedback, base_version: baseVersion }),
        }
      );
      const newA = data.analysis;
      if (state.detail) {
        const rest = (state.detail.analyses || []).filter((a) => a.qa_id !== ctx.qaId);
        state.detail.analyses = rest.concat(data.versions || []);
        state.detail.interview_review_md =
          data.interview_review_md || state.detail.interview_review_md;
      }
      if (state.live) {
        state.live.analyses = (state.live.analyses || [])
          .filter((a) => a.qa_id !== ctx.qaId)
          .concat(data.versions || []);
        state.live.reviewMd = data.interview_review_md || state.live.reviewMd;
      }
      if (newA?.version != null) {
        state.selectedVersions[ctx.qaId] = Number(newA.version);
      }
      toast(`已生成 ${ctx.qaId} V${newA?.version || "?"}`, "ok");
      const modal = document.getElementById("revise-modal");
      if (modal) modal.hidden = true;
      renderResultView();
    } catch (e) {
      toast(e.message || "重新生成失败", "error");
    } finally {
      clearInterval(reviseTimer);
      stopStageTimer();
      state.revising = false;
      if (submit) submit.disabled = false;
      if (cancel) cancel.disabled = false;
    }
  }

  async function togglePauseAnalyze(sessionId) {
    const btn = document.getElementById("btn-pause-analyze");
    if (btn) btn.disabled = true;
    try {
      // 无进行中 SSE（刷新后打开已暂停会话）：重新拉起分析流，后端会跳过已完成题
      if (!state.live || state.live.done) {
        const detail = state.detail || (await api(`/sessions/${encodeURIComponent(sessionId)}`));
        state.detail = detail;
        const qaMd = detail.interview_qa_md || "";
        if (!qaMd.trim()) throw new Error("缺少问答内容，无法继续分析");
        await startAnalyze(sessionId, qaMd);
        return;
      }
      const pausing = !state.live.paused;
      const path = pausing
        ? `/sessions/${encodeURIComponent(sessionId)}/wizard/analyze/pause`
        : `/sessions/${encodeURIComponent(sessionId)}/wizard/analyze/resume`;
      const data = await api(path, { method: "POST" });
      state.live.paused = pausing;
      if (pausing) {
        stopStageTimer();
        state.stageBaseLabel = `已暂停（已完成 ${state.live.completedCount}/${state.live.totalCount || "?"}）`;
        paintStageLabel();
      } else {
        startStageTimer(
          `正在逐题分析（已完成 ${state.live.completedCount}/${state.live.totalCount || "?"}）`,
          3
        );
      }
      toast(data.message || (pausing ? "已暂停" : "已继续"), "ok");
      renderResultView();
    } catch (e) {
      toast(e.message || "操作失败", "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function canJumpQa(vm) {
    // 仅暂停中，或全部分析完毕时可输入题号跳转
    if (vm.paused) return true;
    if (!vm.analyzing) return true;
    const total = vm.totalCount || 0;
    const completed = vm.completedCount || 0;
    return total > 0 && completed >= total;
  }

  function renderPerQaBody(vm) {
    const completed = vm.completedCount || 0;
    if (!completed) {
      return `<div class="empty-state">等待第一题分析完成…</div>`;
    }
    const byQa = groupAnalysesByQa(vm.analyses || []);
    const ordered = (vm.qaItems || []).filter((q) => byQa[q.id]?.length);
    const list =
      ordered.length > 0
        ? ordered.map((q) => {
            const versions = byQa[q.id];
            return { qa: q, versions, analysis: pickAnalysisVersion(q.id, versions) };
          })
        : Object.keys(byQa).map((id) => {
            const versions = byQa[id];
            const analysis = pickAnalysisVersion(id, versions);
            return {
              qa: {
                id,
                topic: "",
                question: "",
                answer: analysis?.original_answer || "",
              },
              versions,
              analysis,
            };
          });

    const idx = Math.min(state.qaIndex, list.length - 1);
    const cur = list[idx];
    const versions = cur.versions || [];
    const curAnalysis = cur.analysis;
    const curVer = Number(curAnalysis?.version) || 1;
    const md = analysisToMarkdown(curAnalysis, cur.qa);
    const atFirst = idx <= 0;
    const atLast = idx >= list.length - 1;
    const jumpable = canJumpQa(vm);
    const revisable = canReviseQa(vm);
    const multi = versions.length > 1;

    const reviseBtn = revisable
      ? `<button type="button" class="version-chip revise-chip" id="btn-revise-qa">输入要求并修改</button>`
      : `<button type="button" class="version-chip revise-chip" id="btn-revise-qa" disabled title="分析完成或暂停后可修改">输入要求并修改</button>`;

    const versionBar =
      versions.length > 0
        ? `<div class="version-bar" aria-label="分析版本与修改">
            <div class="version-bar-left" role="tablist">
              ${versions
                .map((v) => {
                  const ver = Number(v.version) || 1;
                  const active = ver === curVer ? "active" : "";
                  return `<button type="button" class="version-chip ${active}" data-version="${ver}">V${ver}</button>`;
                })
                .join("")}
              ${
                multi
                  ? `<button type="button" class="btn-ghost version-del" id="btn-del-version" title="删除当前版本">删除此版</button>`
                  : ""
              }
            </div>
            ${reviseBtn}
          </div>`
        : "";

    const feedbackNote =
      curAnalysis?.user_feedback
        ? `<p class="version-feedback muted">本版依据用户要求：${escapeHtml(
            curAnalysis.user_feedback
          )}</p>`
        : "";

    const pagerHtml = jumpable
      ? `<div class="qa-pager qa-pager-jump" title="输入题号后回车跳转">
           <input type="number" id="qa-jump-input" class="qa-jump-input" min="1" max="${list.length}" value="${idx + 1}" aria-label="跳转到题号" />
           <span class="qa-pager-sep">/</span>
           <span>${list.length}</span>
         </div>`
      : `<div class="qa-pager">${idx + 1} / ${list.length}</div>`;

    return `
      ${versionBar}
      ${feedbackNote}
      <div class="markdown-body">${renderMarkdown(md)}</div>
      <div class="qa-nav">
        <button type="button" class="nav-arrow" id="nav-prev" ${atFirst ? "disabled" : ""} aria-label="上一题">‹</button>
        ${pagerHtml}
        <button type="button" class="nav-arrow" id="nav-next" ${atLast ? "disabled" : ""} aria-label="下一题">›</button>
      </div>
      ${
        vm.totalCount
          ? `<p class="muted" style="text-align:center;font-size:0.8rem;margin-top:0.5rem">已完成 ${completed} / 共 ${vm.totalCount} 题${
              jumpable ? " · 可输入题号跳转" : " · 分析进行中请用左右翻页"
            }</p>`
          : ""
      }
      <div class="modal-backdrop" id="revise-modal" hidden>
        <div class="modal-card" role="dialog" aria-labelledby="revise-title">
          <h3 id="revise-title">按要求修改本分析结果</h3>
          <p class="hint">说明你不满意之处或希望如何改写；将基于当前查看的 <strong>V${curVer}</strong> 重新生成。</p>
          <textarea id="revise-feedback" rows="6" placeholder="例如：第4段改进回答请更突出用户侧指标，并缩短篇幅…"></textarea>
          <div class="modal-actions">
            <button type="button" class="btn-ghost" id="revise-cancel">取消</button>
            <button type="button" class="btn-primary" id="revise-submit">按要求重新生成</button>
          </div>
          <div class="progress-panel" id="revise-progress"><div id="revise-progress-label"></div></div>
        </div>
      </div>
    `;
  }

  function jumpToQaIndex(targetOneBased, maxCount) {
    const n = Number(targetOneBased);
    if (!Number.isFinite(n) || !Number.isInteger(n)) {
      toast(`请输入 1–${maxCount} 的整数题号`, "error");
      return false;
    }
    if (n < 1 || n > maxCount) {
      toast(`题号需在 1–${maxCount} 之间`, "error");
      return false;
    }
    state.qaIndex = n - 1;
    renderResultView();
    return true;
  }

  async function renameSession(id, currentTitle) {
    if (!id) return;
    const cur =
      (currentTitle != null && String(currentTitle)) ||
      (id === state.activeId || id === state.live?.sessionId
        ? getViewModel()?.title || ""
        : "") ||
      "";
    const title = prompt("输入新的会话标题", cur);
    if (!title || !title.trim()) return;
    try {
      await api(`/sessions/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim() }),
      });
      toast("已重命名", "ok");
      await loadSessions();
      if (state.detail?.meta?.session_id === id) await openSession(id);
      else if (state.live?.sessionId === id) {
        state.live.title = title.trim();
        renderResultView();
      }
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function deleteSession(id) {
    if (!id) return;
    if (!confirm("确定删除该会话？本地文件将一并删除。")) return;
    try {
      await api(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("已删除", "ok");
      const wasActive =
        state.activeId === id || state.live?.sessionId === id || state.detail?.meta?.session_id === id;
      if (wasActive) {
        state.activeId = null;
        state.detail = null;
        state.live = null;
        stopWatchProgress();
        await loadSessions();
        showNewView();
      } else {
        await loadSessions();
      }
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function renameActive() {
    const id = state.activeId || state.live?.sessionId;
    await renameSession(id, getViewModel()?.title || "");
  }

  async function deleteActive() {
    const id = state.activeId || state.live?.sessionId;
    await deleteSession(id);
  }

  // ---------- new / wizard + SSE analyze ----------

  function showNewView() {
    state.activeId = null;
    state.detail = null;
    state.live = null;
    renderSessionList();
    els.topbarTitle.textContent = "开始一次新的面试复盘";
    els.topbarActions.innerHTML = "";

    els.workspace.innerHTML = `
      <div class="hero-compose panel">
        <h2 class="brand-hero">面试复盘助手</h2>
        <p class="lead">上传后将分步转写文稿、澄清问题、生成问答对，每步均可编辑确认，再进入逐题分析。</p>

        <div class="field-block">
          <label for="session-position">面试岗位 <span class="req">*</span></label>
          <div class="hint">必填。将写入逐题分析提示词，用于评价与改进回答。</div>
          <input type="text" id="session-position" placeholder="例如：AI 产品经理" maxlength="100" required />
        </div>

        <form id="review-form" class="upload-grid" autocomplete="off">
          <div class="file-field" data-drop="dialogue">
            <label>面试对话（必填）</label>
            <div class="hint">仅支持 .docx</div>
            <input type="file" id="file-dialogue" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required />
            <div class="file-name" id="name-dialogue"></div>
          </div>
          <div class="file-field" data-drop="resume">
            <label>简历（可选）</label>
            <div class="hint">支持 .pdf / .docx</div>
            <input type="file" id="file-resume" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" />
            <div class="file-name" id="name-resume"></div>
          </div>
        </form>

        <div class="field-block resume-notes-block" id="resume-notes-block">
          <label for="resume-notes">简历注释（可选）</label>
          <div class="hint">写在简历上传下方。补充项目背景、指标口径、文稿里没说清的经历等，帮助 AI 更懂你；不填不影响原流程。开始解析后若与历史不完全相同则存入下方（最多近 10 条），下次可一键回填。</div>
          <textarea id="resume-notes" rows="4" placeholder="例如：某某项目里我负责的评测指标实际是…；离职原因文稿里没写全…"></textarea>
          <div class="notes-history" id="notes-history">
            <div class="notes-history-head">历史注释</div>
            <div class="notes-history-list" id="notes-history-list">
              <div class="notes-history-empty">加载中…</div>
            </div>
          </div>
        </div>

        <div class="field-block">
          <label>岗位 JD（可选）</label>
          <div class="hint">二选一：粘贴纯文本，或上传 .docx。</div>
          <div class="mode-toggle" id="jd-mode-toggle" role="group" aria-label="岗位 JD 输入方式">
            <button type="button" class="mode-btn active" data-jd-mode="text">粘贴文本</button>
            <button type="button" class="mode-btn" data-jd-mode="file">上传文件</button>
          </div>
          <div id="jd-panel-text" class="jd-panel">
            <textarea id="job-description" rows="5" placeholder="粘贴岗位职责与要求…"></textarea>
          </div>
          <div id="jd-panel-file" class="jd-panel" hidden>
            <div class="file-field" data-drop="jd">
              <div class="hint">仅支持 .docx</div>
              <input type="file" id="file-jd" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" />
              <div class="file-name" id="name-jd"></div>
            </div>
          </div>
        </div>

        <div class="field-block">
          <label for="session-title">会话标题（可选）</label>
          <input type="text" id="session-title" placeholder="例如：某厂产品岗一面" maxlength="200" />
        </div>

        <p class="privacy-note">本服务不脱敏。结果保存在本机 data/sessions。</p>

        <div class="form-actions">
          <button type="button" class="btn-primary" id="btn-submit">开始解析</button>
        </div>
        <div class="progress-panel" id="progress-panel">
          <div id="progress-label">准备中…</div>
        </div>
      </div>
    `;

    const dialogueInput = document.getElementById("file-dialogue");
    const resumeInput = document.getElementById("file-resume");
    const jdInput = document.getElementById("file-jd");
    dialogueInput.addEventListener("change", () => {
      document.getElementById("name-dialogue").textContent = dialogueInput.files[0]?.name || "";
    });
    resumeInput.addEventListener("change", () => {
      document.getElementById("name-resume").textContent = resumeInput.files[0]?.name || "";
    });
    jdInput.addEventListener("change", () => {
      document.getElementById("name-jd").textContent = jdInput.files[0]?.name || "";
    });

    setupDrop(
      document.querySelector('[data-drop="dialogue"]'),
      dialogueInput,
      document.getElementById("name-dialogue"),
      [".docx"]
    );
    setupDrop(
      document.querySelector('[data-drop="resume"]'),
      resumeInput,
      document.getElementById("name-resume"),
      [".pdf", ".docx"]
    );
    setupDrop(
      document.querySelector('[data-drop="jd"]'),
      jdInput,
      document.getElementById("name-jd"),
      [".docx"]
    );

    // 岗位 JD：文本 / 文件二选一切换
    state.jdMode = "text";
    document.querySelectorAll("#jd-mode-toggle .mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.jdMode;
        state.jdMode = mode;
        document.querySelectorAll("#jd-mode-toggle .mode-btn").forEach((b) => {
          b.classList.toggle("active", b.dataset.jdMode === mode);
        });
        document.getElementById("jd-panel-text").hidden = mode !== "text";
        document.getElementById("jd-panel-file").hidden = mode !== "file";
        if (mode === "text") {
          jdInput.value = "";
          document.getElementById("name-jd").textContent = "";
        } else {
          const ta = document.getElementById("job-description");
          if (ta) ta.value = "";
        }
      });
    });

    document.getElementById("btn-submit").onclick = () => startWizard();
    loadResumeNotesHistory();
  }

  async function loadResumeNotesHistory() {
    const listEl = document.getElementById("notes-history-list");
    if (!listEl) return;
    try {
      const data = await api("/resume-notes/history?limit=10");
      const items = data.items || [];
      if (!items.length) {
        listEl.innerHTML = `<div class="notes-history-empty">暂无历史。填写注释并开始解析后会自动保存在这里。</div>`;
        return;
      }
      listEl.innerHTML = items
        .map((it) => {
          const preview = escapeHtml(it.preview || it.text || "");
          const when = escapeHtml((it.saved_at || "").replace("T", " ").slice(0, 16));
          return `
            <div class="notes-history-item" data-id="${escapeHtml(it.id)}">
              <button type="button" class="notes-history-pick" title="点击填入注释框">
                <span class="notes-history-preview">${preview}</span>
                <span class="notes-history-meta">${when}</span>
              </button>
              <button type="button" class="notes-history-del" title="删除这条历史" aria-label="删除">×</button>
            </div>`;
        })
        .join("");

      listEl.querySelectorAll(".notes-history-item").forEach((row) => {
        const id = row.dataset.id;
        const full = items.find((x) => x.id === id);
        row.querySelector(".notes-history-pick")?.addEventListener("click", () => {
          const ta = document.getElementById("resume-notes");
          if (!ta || !full) return;
          ta.value = full.text || "";
          ta.focus();
          toast("已填入历史注释", "ok");
        });
        row.querySelector(".notes-history-del")?.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!id) return;
          try {
            await api(`/resume-notes/history/${encodeURIComponent(id)}`, { method: "DELETE" });
            toast("已删除", "ok");
            loadResumeNotesHistory();
          } catch (err) {
            toast(err.message || "删除失败", "error");
          }
        });
      });
    } catch (e) {
      listEl.innerHTML = `<div class="notes-history-empty">历史加载失败：${escapeHtml(
        e.message || String(e)
      )}</div>`;
    }
  }

  function renderWizardStep(step, data) {
    const sid = data.session_id;
    state.activeId = sid;
    let title = "";
    let hint = data.message || "";
    let stageNote = "";
    let content = "";
    let nextLabel = "下一步";
    if (step === "transcript") {
      title = "步骤 1/3 · 转写文稿";
      stageNote =
        "文本澄清阶段1/3，本阶段用于确认文稿大致内容，后续有更详细的澄清阶段，可直接点击确认";
      content = data.cleaned_transcript || "";
      nextLabel = "确认并澄清问题";
    } else if (step === "questions") {
      title = "步骤 2/3 · 澄清问题";
      stageNote =
        "文本澄清阶段2/3，本阶段用于确认面试官所问问题，请确认对应问题以及问题背景是否正确";
      content = data.clarified_questions || "";
      nextLabel = "确认并生成问答对";
    } else {
      title = "步骤 3/3 · 问答对";
      stageNote = "文本澄清阶段3/3，本阶段用于校验问答对，重点关注问题即可";
      content = data.interview_qa_md || "";
      nextLabel = "开始逐题分析";
    }
    els.topbarTitle.textContent = title;
    els.topbarActions.innerHTML = "";
    els.workspace.innerHTML = `
      <div class="panel">
        <p class="wizard-stage-note">【${escapeHtml(stageNote)}】</p>
        ${hint ? `<p class="lead" style="margin-top:0.5rem">${escapeHtml(hint)}</p>` : ""}
        <div class="field-block">
          <label for="wizard-editor">可编辑内容</label>
          <textarea id="wizard-editor" rows="18">${escapeHtml(content)}</textarea>
        </div>
        <div class="form-actions">
          <button type="button" class="btn-primary" id="wizard-next">${escapeHtml(nextLabel)}</button>
        </div>
        <div class="progress-panel" id="progress-panel"><div id="progress-label"></div></div>
      </div>
    `;
    document.getElementById("wizard-next").onclick = () => {
      const text = document.getElementById("wizard-editor")?.value || "";
      if (step === "transcript") confirmTranscript(sid, text);
      else if (step === "questions") confirmQuestions(sid, text);
      else startAnalyze(sid, text);
    };
  }

  async function startWizard() {
    if (state.generating) return;
    const position = document.getElementById("session-position")?.value?.trim() || "";
    const dialogue = document.getElementById("file-dialogue")?.files?.[0];
    const resume = document.getElementById("file-resume")?.files?.[0];
    const resumeNotes = document.getElementById("resume-notes")?.value?.trim() || "";
    const jdMode = state.jdMode || "text";
    const jdFile =
      jdMode === "file" ? document.getElementById("file-jd")?.files?.[0] : null;
    const jdText =
      jdMode === "text"
        ? document.getElementById("job-description")?.value?.trim() || ""
        : "";
    const title = document.getElementById("session-title")?.value?.trim() || "";
    const btn = document.getElementById("btn-submit");
    const panel = document.getElementById("progress-panel");
    const lab = document.getElementById("progress-label");

    if (!position) {
      toast("请填写面试岗位", "error");
      return;
    }
    if (!dialogue) {
      toast("请先选择面试对话 .docx", "error");
      return;
    }

    const fd = new FormData();
    fd.append("dialogue", dialogue);
    fd.append("position", position);
    if (resume) fd.append("resume", resume);
    if (resumeNotes) fd.append("resume_notes", resumeNotes);
    if (jdText) fd.append("job_description", jdText);
    if (jdFile) fd.append("jd_file", jdFile);
    if (title) fd.append("title", title);

    state.generating = true;
    if (btn) btn.disabled = true;
    if (panel) panel.classList.add("visible");
    startStageTimer("正在转写文稿（含简历摘要）");

    try {
      const res = await fetch(`${API}/sessions/wizard/start`, { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      stopStageTimer();
      await loadSessions();
      renderWizardStep("transcript", data);
      toast("转写完成，请核对", "ok");
    } catch (e) {
      stopStageTimer();
      toast(e.message || "启动失败", "error");
      if (lab) lab.textContent = `失败：${e.message}`;
    } finally {
      state.generating = false;
      if (btn) btn.disabled = false;
    }
  }

  async function confirmTranscript(sessionId, content) {
    if (state.generating) return;
    if (!content.trim()) {
      toast("转写文稿不能为空", "error");
      return;
    }
    state.generating = true;
    const btn = document.getElementById("wizard-next");
    const lab = document.getElementById("progress-label");
    const panel = document.getElementById("progress-panel");
    if (btn) btn.disabled = true;
    if (panel) panel.classList.add("visible");
    startStageTimer("正在澄清问题");
    try {
      const data = await api(`/sessions/${encodeURIComponent(sessionId)}/wizard/confirm-transcript`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      stopStageTimer();
      await loadSessions();
      renderWizardStep("questions", data);
      toast("问题澄清完成，请核对", "ok");
    } catch (e) {
      stopStageTimer();
      toast(e.message, "error");
      if (lab) lab.textContent = `失败：${e.message}`;
      if (btn) btn.disabled = false;
    } finally {
      state.generating = false;
    }
  }

  async function confirmQuestions(sessionId, content) {
    if (state.generating) return;
    if (!content.trim()) {
      toast("问题列表不能为空", "error");
      return;
    }
    state.generating = true;
    const btn = document.getElementById("wizard-next");
    const lab = document.getElementById("progress-label");
    const panel = document.getElementById("progress-panel");
    if (btn) btn.disabled = true;
    if (panel) panel.classList.add("visible");
    startStageTimer("正在生成问答对");
    try {
      const data = await api(`/sessions/${encodeURIComponent(sessionId)}/wizard/confirm-questions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      stopStageTimer();
      await loadSessions();
      renderWizardStep("qa", data);
      toast("问答对已生成，请核对", "ok");
    } catch (e) {
      stopStageTimer();
      toast(e.message, "error");
      if (lab) lab.textContent = `失败：${e.message}`;
      if (btn) btn.disabled = false;
    } finally {
      state.generating = false;
    }
  }

  async function startAnalyze(sessionId, qaMd) {
    if (state.generating) return;
    if (!qaMd.trim()) {
      toast("问答内容不能为空", "error");
      return;
    }
    state.generating = true;
    const btn = document.getElementById("wizard-next");
    if (btn) btn.disabled = true;

    state.live = {
      sessionId,
      title: state.detail?.meta?.title || "面试复盘",
      position: state.detail?.meta?.position || "",
      qaItems: [],
      analyses: [],
      completedCount: 0,
      totalCount: 0,
      reviewMd: "",
      jd: state.detail?.job_description || "",
      warnings: [],
      done: false,
      paused: false,
    };
    // 若 detail 未加载，先拉一次 meta
    try {
      const detail = await api(`/sessions/${encodeURIComponent(sessionId)}`);
      state.detail = detail;
      state.live.title = detail.meta?.title || state.live.title;
      state.live.position = detail.meta?.position || state.live.position;
      state.live.jd = detail.job_description || "";
    } catch (_) {}

    state.tab = "per_qa";
    state.qaIndex = 0;
    let enteredResult = false;

    els.workspace.innerHTML = `
      <div class="panel">
        <div class="progress-panel visible" id="progress-panel">
          <div id="progress-label"></div>
          <ul class="steps" id="progress-steps">
            <li data-step="3" class="active">3/4 逐题分析</li>
            <li data-step="4">4/4 合并结果</li>
          </ul>
        </div>
        <div class="empty-state">等待第一题分析完成…</div>
      </div>
    `;
    startStageTimer("正在逐题分析", 3);

    try {
      const res = await fetch(`${API}/sessions/${encodeURIComponent(sessionId)}/wizard/analyze/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: qaMd }),
      });
      if (!res.ok) {
        let detail = `请求失败 HTTP ${res.status}`;
        try {
          const j = await res.json();
          detail = j.detail || detail;
        } catch (_) {}
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      const onEnter = () => {
        if (!enteredResult) {
          enteredResult = true;
          state.activeId = state.live.sessionId;
          renderResultView();
          loadSessions();
        } else {
          renderResultView();
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const chunk of parts) {
          const err = handleSseChunk(chunk, { onEnterResult: onEnter });
          if (err) throw new Error(err);
        }
      }
      if (buffer.trim()) {
        const err = handleSseChunk(buffer, { onEnterResult: onEnter });
        if (err) throw new Error(err);
      }

      if (state.live?.done && state.live.sessionId) {
        stopStageTimer();
        toast("生成完成", "ok");
        await openSession(state.live.sessionId);
      }
    } catch (e) {
      stopStageTimer();
      toast(e.message || "分析失败", "error");
    } finally {
      state.generating = false;
    }
  }

  function setupDrop(zone, input, nameEl, exts) {
    if (!zone) return;
    ["dragenter", "dragover"].forEach((ev) => {
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((ev) => {
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.remove("dragover");
      });
    });
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files?.[0];
      if (!file) return;
      const lower = file.name.toLowerCase();
      if (!exts.some((x) => lower.endsWith(x))) {
        toast(`请上传 ${exts.join(" / ")} 文件`, "error");
        return;
      }
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      nameEl.textContent = file.name;
    });
  }

  function setProgressUI(step, label, skipped) {
    const panel = document.getElementById("progress-panel");
    if (panel) panel.classList.add("visible");
    const base = skipped
      ? `${label}（${step}/4，已跳过）`
      : `${label}（${step}/4）`;
    if (state.stageStep !== step) {
      startStageTimer(base, step);
    } else {
      updateStageLabel(base);
    }
    document.querySelectorAll("#progress-steps li").forEach((li) => {
      const s = Number(li.dataset.step);
      li.classList.toggle("active", s === step);
      li.classList.toggle("done", s < step);
    });
  }

  /** @returns {string|null} error message if error event */
  function handleSseChunk(chunk, { onEnterResult }) {
    const lines = chunk.split("\n");
    let eventType = "message";
    let dataLine = "";
    for (const line of lines) {
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLine += line.slice(5).trim();
    }
    if (!dataLine) return null;
    let data;
    try {
      data = JSON.parse(dataLine);
    } catch {
      return null;
    }
    const type = data.type || eventType;

    if (type === "session") {
      state.live.sessionId = data.session_id;
      state.live.title = data.title || state.live.title;
      state.live.position = data.position || state.live.position;
    } else if (type === "progress") {
      if (state.live) state.live.paused = false;
      setProgressUI(data.step, data.label || "", !!data.skipped);
    } else if (type === "qa_done") {
      if (!state.live.analyses) state.live.analyses = [];
      const existing = state.live.analyses.findIndex((a) => a.qa_id === data.analysis.qa_id);
      if (existing >= 0) state.live.analyses[existing] = data.analysis;
      else state.live.analyses.push(data.analysis);

      if (data.qa) {
        const qi = state.live.qaItems.findIndex((q) => q.id === data.qa.id);
        if (qi >= 0) state.live.qaItems[qi] = data.qa;
        else state.live.qaItems.push(data.qa);
      }
      state.live.completedCount = data.completed_count;
      state.live.totalCount = data.total_count;
      if (!state.live.paused) {
        updateStageLabel(
          `正在逐题分析（已完成 ${data.completed_count}/${data.total_count}）`
        );
      }
      onEnterResult();
    } else if (type === "paused") {
      state.live.paused = true;
      state.live.completedCount = data.completed_count ?? state.live.completedCount;
      state.live.totalCount = data.total_count ?? state.live.totalCount;
      stopStageTimer();
      state.stageBaseLabel = `已暂停（已完成 ${state.live.completedCount}/${state.live.totalCount || "?"}）`;
      paintStageLabel();
      onEnterResult();
    } else if (type === "resumed") {
      state.live.paused = false;
      state.live.completedCount = data.completed_count ?? state.live.completedCount;
      state.live.totalCount = data.total_count ?? state.live.totalCount;
      startStageTimer(
        `正在逐题分析（已完成 ${state.live.completedCount}/${state.live.totalCount || "?"}）`,
        3
      );
      onEnterResult();
    } else if (type === "done") {
      state.live.done = true;
      state.live.paused = false;
      state.live.reviewMd = data.interview_review_md || "";
      state.live.warnings = data.warnings || [];
      state.live.title = data.title || state.live.title;
      setProgressUI(4, "正在合并结果", false);
      onEnterResult();
    } else if (type === "error") {
      stopStageTimer();
      return data.message || "分析失败";
    }
    return null;
  }

  // ---------- boot ----------

  els.btnNew.addEventListener("click", () => {
    showNewView();
    closeSidebarOnMobile();
  });
  els.btnToggle?.addEventListener("click", () => {
    els.sidebar.classList.toggle("open");
  });

  const E2E_DEFAULT = {
    position: "AI产品经理",
    title: "默认测试1",
    resumeNotes:
      "度小满的AI客服项目，我参与了整个产品的框架构建（用户识别层-业务执行层-服务保障层）工作，同时在迭代阶段，通过badcase的发现，推动了几个子模块的上线（如：安全模块，从输入输出两端进行检测，无违规信息才放行，有违规则改为兜底话术；首次进行判断模块：再次进线距离上次进线时间太长或太短，直接走固定规则新开会话或延续之前会话，如果处于中间态，则采用模型语义判断的方式；转人工监管模型：独立的审查此时的对话是否应该转人工的模块，与主对话模型一起判断，提高应转问题的召回率）。\n" +
      "新开普AI生成教学动画的项目调研环节的竞品有oiioii、可灵、即梦。调研的目的是确定视频生成产品从输入到输出的功能流程。最终模仿了oiioii较为细致的工作流，同时做出了针对教学场景的改造，如添加了“预对话”步骤收集参数（如学生年级、知识学科）用于辅助模型锁定知识点范围，减少错误风险。",
    dialogueUrl: "/static/e2e_fixtures/dialogue.docx",
    dialogueName: "懂车帝二面.docx",
    resumeUrl: "/static/e2e_fixtures/resume.docx",
    resumeName: "测试简历.docx",
  };

  async function assignFileInput(inputEl, nameEl, url, filename) {
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`无法加载测试文件 ${url}（HTTP ${res.status}）`);
    }
    const blob = await res.blob();
    const file = new File([blob], filename, {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    const dt = new DataTransfer();
    dt.items.add(file);
    inputEl.files = dt.files;
    if (nameEl) nameEl.textContent = filename;
    inputEl.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function runDefaultE2EAutofill() {
    /** 测试脚本打开 ?e2e=default 时：填表并点「开始解析」，之后交给用户。 */
    showNewView();
    await new Promise((r) => setTimeout(r, 80));

    const positionEl = document.getElementById("session-position");
    const titleEl = document.getElementById("session-title");
    const notesEl = document.getElementById("resume-notes");
    const jdEl = document.getElementById("job-description");
    const dialogueInput = document.getElementById("file-dialogue");
    const resumeInput = document.getElementById("file-resume");
    const submitBtn = document.getElementById("btn-submit");

    if (!positionEl || !dialogueInput || !submitBtn) {
      throw new Error("新建页表单未就绪");
    }

    positionEl.value = E2E_DEFAULT.position;
    if (titleEl) titleEl.value = E2E_DEFAULT.title;
    if (notesEl) notesEl.value = E2E_DEFAULT.resumeNotes;
    if (jdEl) jdEl.value = "";

    await assignFileInput(
      dialogueInput,
      document.getElementById("name-dialogue"),
      E2E_DEFAULT.dialogueUrl,
      E2E_DEFAULT.dialogueName
    );
    await assignFileInput(
      resumeInput,
      document.getElementById("name-resume"),
      E2E_DEFAULT.resumeUrl,
      E2E_DEFAULT.resumeName
    );

    // 去掉 e2e 参数，避免刷新重复提交；保留页面给用户继续操作
    history.replaceState({}, "", "/");
    toast("测试脚本已自动填表，正在点击「开始解析」…", "ok");
    submitBtn.click();
  }

  async function boot() {
    showNewView();
    try {
      await loadSessions();
      const params = new URLSearchParams(location.search);
      const sid = params.get("session");
      const e2e = params.get("e2e");
      if (e2e === "default") {
        await runDefaultE2EAutofill();
        return;
      }
      if (sid) {
        await openSession(sid);
      }
    } catch (e) {
      toast(e.message || "无法连接本地服务，请先启动后端", "error");
      console.error(e);
    }
  }

  boot();
})();
