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
    // 阶段读秒（需求4）
    stageBaseLabel: "",
    stageStartedAt: 0,
    stageTimerId: null,
    stageStep: null,
  };

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
        return `
          <button type="button" class="session-item ${active}" data-id="${escapeHtml(s.session_id)}" role="listitem">
            <div class="t">${escapeHtml(s.title || s.session_id)}</div>
            <div class="m">
              <span>${escapeHtml(statusLabel(s.status))}</span>
              <span>${s.qa_count || 0} 题</span>
              <span>${escapeHtml(formatTime(s.updated_at))}</span>
            </div>
          </button>`;
      })
      .join("");
    list.querySelectorAll(".session-item").forEach((btn) => {
      btn.addEventListener("click", () => openSession(btn.dataset.id));
    });
  }

  async function openSession(id) {
    try {
      state.activeId = id;
      state.live = null;
      state.tab = "per_qa";
      state.qaIndex = 0;
      renderSessionList();
      els.workspace.innerHTML = `<div class="panel empty-state">加载中…</div>`;
      const detail = await api(`/sessions/${encodeURIComponent(id)}`);
      state.detail = detail;
      const step = detail.meta?.workflow_step || "";
      const st = detail.meta?.status || "";
      // 未完成的解析向导：回到对应确认页
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
    return {
      sessionId: m.session_id,
      title: m.title,
      position: m.position || "待确认",
      status: m.status,
      qaItems: d.qa_items || [],
      analyses: d.analyses || [],
      completedCount: (d.analyses || []).length,
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
    }

    const pauseBtn = document.getElementById("btn-pause-analyze");
    if (pauseBtn) {
      pauseBtn.onclick = () => togglePauseAnalyze(vm.sessionId);
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

  function renderPerQaBody(vm) {
    const completed = vm.completedCount || 0;
    if (!completed) {
      return `<div class="empty-state">等待第一题分析完成…</div>`;
    }
    // Map analyses by qa_id; display in qa_items order among completed
    const byId = {};
    (vm.analyses || []).forEach((a) => {
      byId[a.qa_id] = a;
    });
    const ordered = (vm.qaItems || []).filter((q) => byId[q.id]);
    // If live mode analyses arrive before qaItems synced, fall back to analyses order
    const list =
      ordered.length > 0
        ? ordered.map((q) => ({ qa: q, analysis: byId[q.id] }))
        : (vm.analyses || []).map((a) => ({
            qa: { id: a.qa_id, topic: "", question: "", answer: a.original_answer },
            analysis: a,
          }));

    const idx = Math.min(state.qaIndex, list.length - 1);
    const cur = list[idx];
    const md = analysisToMarkdown(cur.analysis, cur.qa);
    const atFirst = idx <= 0;
    const atLast = idx >= list.length - 1;

    return `
      <div class="markdown-body">${renderMarkdown(md)}</div>
      <div class="qa-nav">
        <button type="button" class="nav-arrow" id="nav-prev" ${atFirst ? "disabled" : ""} aria-label="上一题">‹</button>
        <div class="qa-pager">${idx + 1} / ${list.length}</div>
        <button type="button" class="nav-arrow" id="nav-next" ${atLast ? "disabled" : ""} aria-label="下一题">›</button>
      </div>
      ${
        vm.totalCount
          ? `<p class="muted" style="text-align:center;font-size:0.8rem;margin-top:0.5rem">已完成 ${completed} / 共 ${vm.totalCount} 题</p>`
          : ""
      }
    `;
  }

  async function renameActive() {
    const id = state.activeId || state.live?.sessionId;
    if (!id) return;
    const cur = getViewModel()?.title || "";
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
      if (state.detail) await openSession(id);
      else if (state.live) {
        state.live.title = title.trim();
        renderResultView();
      }
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function deleteActive() {
    const id = state.activeId || state.live?.sessionId;
    if (!id) return;
    if (!confirm("确定删除该会话？本地文件将一并删除。")) return;
    try {
      await api(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("已删除", "ok");
      state.activeId = null;
      state.detail = null;
      state.live = null;
      await loadSessions();
      showNewView();
    } catch (e) {
      toast(e.message, "error");
    }
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
          <div class="hint">写在简历上传下方。补充项目背景、指标口径、文稿里没说清的经历等，帮助 AI 更懂你；不填不影响原流程。</div>
          <textarea id="resume-notes" rows="4" placeholder="例如：某某项目里我负责的评测指标实际是…；离职原因文稿里没写全…"></textarea>
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

  async function boot() {
    showNewView();
    try {
      await loadSessions();
    } catch (e) {
      toast("无法连接本地服务，请先启动后端", "error");
      console.error(e);
    }
  }

  boot();
})();
