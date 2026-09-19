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
    const audio = $("#meetingAudio");
    if (meeting?.audio_url) { audio.href = meeting.audio_url; audio.hidden = false; } else { audio.hidden = true; }
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

  setDefaults();
  refresh();
  window.setInterval(refresh, 1500);
}
