const $ = (selector) => document.querySelector(selector);

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
  return payload;
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

function clock(seconds) {
  const value = Math.max(0, Math.floor(seconds || 0));
  const hours = String(Math.floor(value / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((value % 3600) / 60)).padStart(2, "0");
  const secs = String(value % 60).padStart(2, "0");
  return `${hours}:${minutes}:${secs}`;
}

function sourceLabel(source) {
  return { local: "智能助手", macos: "Mac 日历", phone: "手机", feishu: "飞书", wecom: "企微" }[source] || source;
}

export function initializeAssistant() {
  const panel = $("#assistantPanel");
  if (!panel) return;
  const agenda = $("#assistantAgenda");
  const agendaResult = $("#assistantAgendaResult");
  const form = $("#assistantEventForm");
  let pending = false;

  const setDefaults = () => {
    const start = new Date(Date.now() + 30 * 60 * 1000);
    start.setSeconds(0, 0);
    const end = new Date(start.getTime() + 60 * 60 * 1000);
    const local = (date) => new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    form.elements.start.value = local(start);
    form.elements.end.value = local(end);
  };

  async function action(eventId, action, minutes = 10) {
    await request(`/api/assistant/events/${encodeURIComponent(eventId)}/action`, {
      method: "POST", body: JSON.stringify({ action, minutes }),
    });
    await refresh();
  }

  function renderAgenda(events) {
    agenda.replaceChildren();
    for (const event of events) {
      const row = document.createElement("div");
      row.className = "assistant-event";
      row.dataset.state = event.reminder_state || "pending";
      const duplicate = event.possible_duplicate ? "<b>疑似跨源重复</b>" : "";
      row.innerHTML = `<div><span>${sourceLabel(event.source)} · ${formatDate(event.start)}</span><strong></strong><small>${event.write_back ? "可回写" : "只读 / 本地提醒"} ${duplicate}</small></div><div class="assistant-event-actions"></div>`;
      row.querySelector("strong").textContent = event.title;
      const controls = row.querySelector(".assistant-event-actions");
      const snooze = document.createElement("button");
      snooze.type = "button"; snooze.textContent = "延后 10 分钟";
      snooze.addEventListener("click", () => action(event.id, "snooze"));
      const done = document.createElement("button");
      done.type = "button"; done.textContent = "完成";
      done.addEventListener("click", () => action(event.id, "done"));
      controls.append(snooze, done);
      agenda.append(row);
    }
    if (!events.length) agenda.textContent = "未来 48 小时没有日程。";
  }

  function renderMeeting(meeting) {
    const active = meeting && ["starting", "recording", "stopping"].includes(meeting.state);
    $("#meetingCard").dataset.recording = String(Boolean(active));
    $("#meetingBadge").textContent = active ? meeting.state.toUpperCase() : (meeting?.state || "IDLE").toUpperCase();
    $("#meetingClock").textContent = clock(meeting?.elapsed_seconds || meeting?.duration_seconds || 0);
    $("#startMeeting").disabled = Boolean(active);
    $("#stopMeeting").disabled = !active;
    panel.querySelectorAll("[data-meeting-mark]").forEach((button) => { button.disabled = !active; });
    const result = $("#meetingResult");
    result.textContent = meeting ? `${meeting.title} · ${meeting.state}${meeting.transcript_state ? ` · ${meeting.transcript_state}` : ""}` : "未开始录音";
    result.dataset.tone = meeting?.state === "failed" ? "error" : meeting?.state === "completed" ? "ok" : "";
    const tracking = $("#meetingTrackingState");
    const trackingState = meeting?.face_tracking_state || "idle";
    tracking.dataset.state = trackingState;
    tracking.textContent = {
      running: "人脸跟随：运行中，机器人会持续面向你",
      already_running: "人脸跟随：沿用会议前已开启的跟随",
      stopped: "人脸跟随：已随会议结束停止",
      stop_required: "人脸跟随：停止待重试",
      unsupported: "人脸跟随：当前固件不支持",
      idle: "人脸跟随：等待会议开始",
    }[trackingState] || `人脸跟随：${trackingState}`;
    const audio = $("#meetingAudio");
    if (meeting?.audio_url) { audio.href = meeting.audio_url; audio.hidden = false; } else { audio.hidden = true; }
    const processingState = $("#meetingProcessingState");
    if (!meeting) {
      processingState.textContent = "录音停止后显示转写与纪要状态";
    } else if (meeting.state === "recording") {
      processingState.textContent = "正在录音，停止后将自动进入本地处理";
    } else {
      processingState.textContent = `转写：${meeting.transcript_state || "待处理"} · 纪要：${meeting.evaluation_state || "待处理"}`;
    }
    const transcript = $("#meetingTranscript");
    const summary = $("#meetingSummary");
    if (meeting?.transcript_url) { transcript.href = meeting.transcript_url; transcript.hidden = false; } else { transcript.hidden = true; }
    if (meeting?.summary_preview_url || meeting?.summary_url) {
      summary.href = meeting.summary_preview_url || meeting.summary_url;
      summary.hidden = false;
    } else { summary.hidden = true; }
    renderActionItems(meeting);
  }

  function renderActionItems(meeting) {
    const container = $("#meetingActionItems");
    container.replaceChildren();
    const items = meeting?.action_items || [];
    if (!items.length) { container.textContent = "暂无行动项"; return; }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "meeting-action-item";
      row.dataset.state = item.state;
      const copy = document.createElement("div");
      const title = document.createElement("strong"); title.textContent = item.title;
      const meta = document.createElement("small");
      const origin = item.source === "transcript_summary" ? "真实转写提取" : clock(item.elapsed_seconds);
      const owner = item.owner ? ` · 责任人：${item.owner}` : "";
      const due = item.due ? ` · 截止：${item.due}` : "";
      meta.textContent = `${origin}${owner}${due} · ${item.state === "pending_confirmation" ? "待确认" : item.state === "confirmed_local" ? "已确认（未外部回写）" : "已忽略"}`;
      copy.append(title, meta); row.append(copy);
      if (item.state === "pending_confirmation") {
        const controls = document.createElement("div");
        for (const [action, label] of [["confirm", "确认"], ["dismiss", "忽略"]]) {
          const button = document.createElement("button"); button.type = "button"; button.textContent = label;
          button.addEventListener("click", async () => {
            await request(`/api/assistant/meeting/${encodeURIComponent(meeting.id)}/actions/${encodeURIComponent(item.id)}`, {
              method: "POST", body: JSON.stringify({ action }),
            });
            await refresh();
          });
          controls.append(button);
        }
        row.append(controls);
      }
      container.append(row);
    }
  }

  function renderDemo(status) {
    const meeting = status.meeting;
    const hasDemoAgenda = (status.agenda || []).some((event) => event.title.includes("演示"));
    const hasMark = Boolean(meeting?.marks?.length);
    const processed = meeting?.state === "completed" || meeting?.state === "failed";
    const states = {
      agenda: hasDemoAgenda ? "complete" : "ready",
      record: meeting ? (status.recording_active ? "active" : "complete") : "ready",
      mark: hasMark ? "complete" : (status.recording_active ? "ready" : "locked"),
      process: processed ? "complete" : (meeting?.state === "stopping" ? "active" : "locked"),
    };
    for (const [step, state] of Object.entries(states)) {
      const element = panel.querySelector(`[data-demo-step="${step}"]`);
      if (element) element.dataset.state = state;
    }
    $("#demoStartMeeting").disabled = Boolean(status.recording_active);
    $("#demoMarkTodo").disabled = !status.recording_active;
    $("#demoStopMeeting").disabled = !status.recording_active;
  }

  async function refresh() {
    if (pending) return;
    pending = true;
    try {
      const status = await request("/api/assistant/status");
      $("#assistantState").textContent = status.recording_active ? "会议录音中" : "已连接";
      $("#assistantAsrState").textContent = status.asr.message;
      renderAgenda(status.agenda || []);
      renderMeeting(status.meeting);
      renderDemo(status);
      agendaResult.textContent = `已加载 ${status.agenda?.length || 0} 个日程`;
      agendaResult.dataset.tone = "ok";
    } catch (error) {
      $("#assistantState").textContent = "连接失败";
      agendaResult.textContent = error.message;
      agendaResult.dataset.tone = "error";
    } finally { pending = false; }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      await request("/api/assistant/events", { method: "POST", body: JSON.stringify({
        source: "local", calendar_id: "smart-assistant", title: data.get("title"),
        start: new Date(data.get("start")).toISOString(), end: new Date(data.get("end")).toISOString(),
        reminder_minutes: Number(data.get("reminder_minutes")), sensitive: data.get("sensitive") === "on",
        presence_check: true, write_back: true,
      }) });
      form.elements.title.value = "";
      agendaResult.textContent = "已添加日程";
      await refresh();
    } catch (error) { agendaResult.textContent = error.message; agendaResult.dataset.tone = "error"; }
  });

  $("#syncMacCalendar").addEventListener("click", async () => {
    agendaResult.textContent = "正在读取 Calendar.app…";
    try {
      const result = await request("/api/assistant/calendar/macos/sync?hours=48", { method: "POST" });
      agendaResult.textContent = `已同步 ${result.count} 个日程（只读）`;
      await refresh();
    } catch (error) { agendaResult.textContent = error.message; agendaResult.dataset.tone = "error"; }
  });

  $("#startMeeting").addEventListener("click", async () => {
    try {
      const result = await request("/api/assistant/meeting/start", { method: "POST", body: JSON.stringify({
        title: $("#meetingTitle").value || "未命名会议", consent_statement: $("#meetingConsent").value,
        max_duration_minutes: 120,
      }) });
      renderMeeting(result.meeting);
    } catch (error) { $("#meetingResult").textContent = error.message; $("#meetingResult").dataset.tone = "error"; }
  });
  $("#stopMeeting").addEventListener("click", async () => {
    try { await request("/api/assistant/meeting/stop", { method: "POST" }); await refresh(); }
    catch (error) { $("#meetingResult").textContent = error.message; }
  });
  panel.querySelectorAll("[data-meeting-mark]").forEach((button) => button.addEventListener("click", async () => {
    try {
      await request("/api/assistant/meeting/mark", { method: "POST", body: JSON.stringify({ label: button.dataset.meetingMark, note: "" }) });
      $("#meetingResult").textContent = `已标记：${button.dataset.meetingMark}`;
    } catch (error) { $("#meetingResult").textContent = error.message; }
  }));

  $("#demoLoadAgenda").addEventListener("click", async () => {
    const output = $("#demoJourneyResult"); output.textContent = "正在载入演示议程…";
    try {
      const result = await request("/api/assistant/demo/bootstrap", { method: "POST" });
      $("#meetingTitle").value = "产品周会（演示）";
      output.textContent = result.message;
      await refresh();
    } catch (error) { output.textContent = error.message; output.dataset.tone = "error"; }
  });
  $("#demoStartMeeting").addEventListener("click", () => $("#startMeeting").click());
  $("#demoMarkTodo").addEventListener("click", async () => {
    const output = $("#demoJourneyResult");
    try {
      const result = await request("/api/assistant/meeting/mark", { method: "POST", body: JSON.stringify({
        label: "待办", note: "",
      }) });
      output.textContent = `已在 ${clock(result.mark.elapsed_seconds)} 留下时间锚点；行动项将从真实转写中提取`;
      renderMeeting(result.meeting);
    } catch (error) { output.textContent = error.message; output.dataset.tone = "error"; }
  });
  $("#demoStopMeeting").addEventListener("click", () => {
    $("#demoJourneyResult").textContent = "正在停止录音并进入会后处理…";
    $("#stopMeeting").click();
  });

  setDefaults();
  refresh();
  window.setInterval(refresh, 1500);
}
