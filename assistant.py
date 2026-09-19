"""Persistent agenda and meeting metadata for the desktop assistant MVP."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


EVENT_SOURCES = {"local", "macos", "phone", "feishu", "wecom"}
_TITLE_KEY = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("datetime value is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("datetime must use ISO 8601 format") from error
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _title_key(value: str) -> str:
    return _TITLE_KEY.sub("", value.casefold())


class AssistantStore:
    """Small, local-first store with explicit source and write-back semantics."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.meetings_dir = self.root / "meetings"
        self.meetings_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self.root / "events.json"
        self._meetings_path = self.root / "meetings.json"
        self._audit_path = self.root / "audit.jsonl"
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = self._load_list(self._events_path)
        self._meetings: list[dict[str, Any]] = self._load_list(self._meetings_path)

    @staticmethod
    def _load_list(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _atomic_write(path: Path, value: object) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _save_events(self) -> None:
        self._atomic_write(self._events_path, self._events)

    def _save_meetings(self) -> None:
        self._atomic_write(self._meetings_path, self._meetings)

    def audit(self, action: str, details: Mapping[str, object]) -> None:
        entry = {"at": _iso(_now()), "action": action, "details": dict(details)}
        with self._lock:
            with self._audit_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def upsert_event(self, payload: Mapping[str, object]) -> dict[str, Any]:
        source = str(payload.get("source") or "local").strip().lower()
        if source not in EVENT_SOURCES:
            raise ValueError(f"unsupported source: {source}")
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 160:
            raise ValueError("title must contain 1 to 160 characters")
        start = parse_datetime(str(payload.get("start") or ""))
        end = parse_datetime(str(payload.get("end") or ""))
        if end <= start:
            raise ValueError("end must be after start")
        reminder_minutes = int(payload.get("reminder_minutes") or 15)
        if reminder_minutes < 0 or reminder_minutes > 10_080:
            raise ValueError("reminder_minutes must be between 0 and 10080")
        external_id = str(payload.get("external_event_id") or "").strip()
        calendar_id = str(payload.get("calendar_id") or source).strip()[:160]
        event_id = str(payload.get("id") or "").strip()
        if not event_id:
            stable = f"{source}:{calendar_id}:{external_id}" if external_id else ""
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable)) if stable else str(uuid.uuid4())
        now = _iso(_now())
        event = {
            "id": event_id,
            "source": source,
            "external_event_id": external_id or event_id,
            "calendar_id": calendar_id,
            "title": title,
            "start": _iso(start),
            "end": _iso(end),
            "timezone": str(payload.get("timezone") or datetime.now().astimezone().tzinfo),
            "updated_at": str(payload.get("updated_at") or now),
            "write_back": source == "local" or bool(payload.get("write_back", False)),
            "description": str(payload.get("description") or "")[:2000],
            "sensitive": bool(payload.get("sensitive", False)),
            "reminder_minutes": reminder_minutes,
            "presence_check": bool(payload.get("presence_check", True)),
            "status": str(payload.get("status") or "scheduled"),
            "reminder_state": str(payload.get("reminder_state") or "pending"),
            "reminder_attempts": int(payload.get("reminder_attempts") or 0),
        }
        with self._lock:
            for index, existing in enumerate(self._events):
                if existing.get("id") == event_id:
                    event["reminder_state"] = str(
                        payload.get("reminder_state") or existing.get("reminder_state") or "pending"
                    )
                    event["reminder_attempts"] = int(
                        payload.get("reminder_attempts") or existing.get("reminder_attempts") or 0
                    )
                    self._events[index] = event
                    break
            else:
                self._events.append(event)
            self._mark_possible_duplicates_locked()
            self._save_events()
        self.audit("agenda.event_upserted", {"id": event_id, "source": source})
        return dict(event)

    def import_events(self, source: str, items: Iterable[Mapping[str, object]]) -> list[dict[str, Any]]:
        source = source.strip().lower()
        if source not in EVENT_SOURCES - {"local"}:
            raise ValueError("import source must be macos, phone, feishu, or wecom")
        imported = []
        for item in items:
            merged = dict(item)
            merged["source"] = source
            merged.setdefault("write_back", False)
            imported.append(self.upsert_event(merged))
        self.audit("agenda.source_imported", {"source": source, "count": len(imported)})
        return imported

    def list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self._lock:
            result = [
                dict(event)
                for event in self._events
                if parse_datetime(str(event["end"])) >= start
                and parse_datetime(str(event["start"])) <= end
                and event.get("status") != "deleted"
            ]
        return sorted(result, key=lambda item: (str(item["start"]), str(item["title"])))

    def _mark_possible_duplicates_locked(self) -> None:
        for event in self._events:
            event["possible_duplicate"] = False
        for index, event in enumerate(self._events):
            start = parse_datetime(str(event["start"]))
            for other in self._events[index + 1 :]:
                if event.get("source") == other.get("source"):
                    continue
                if _title_key(str(event.get("title") or "")) != _title_key(str(other.get("title") or "")):
                    continue
                if abs((start - parse_datetime(str(other["start"]))).total_seconds()) <= 120:
                    event["possible_duplicate"] = True
                    other["possible_duplicate"] = True

    def update_event_state(self, event_id: str, action: str, minutes: int = 10) -> dict[str, Any]:
        with self._lock:
            event = next((item for item in self._events if item.get("id") == event_id), None)
            if event is None:
                raise KeyError(event_id)
            if action == "done":
                event["status"] = "completed"
                event["reminder_state"] = "completed"
            elif action == "snooze":
                if minutes < 1 or minutes > 1440:
                    raise ValueError("snooze minutes must be between 1 and 1440")
                event["snoozed_until"] = _iso(_now() + timedelta(minutes=minutes))
                event["reminder_state"] = "pending"
            else:
                raise ValueError("action must be done or snooze")
            event["updated_at"] = _iso(_now())
            self._save_events()
            result = dict(event)
        self.audit("agenda.event_state_changed", {"id": event_id, "action": action})
        return result

    def event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = next((item for item in self._events if item.get("id") == event_id), None)
            return dict(value) if value else None

    def queue_reminder_now(self, event_id: str) -> dict[str, Any]:
        with self._lock:
            event = next((item for item in self._events if item.get("id") == event_id), None)
            if event is None:
                raise KeyError(event_id)
            event["snoozed_until"] = _iso(_now() - timedelta(seconds=1))
            event["reminder_state"] = "pending"
            event["reminder_attempts"] = 0
            self._save_events()
            return dict(event)

    def claim_due_reminders(self, *, recording_active: bool) -> list[dict[str, Any]]:
        now = _now()
        due: list[dict[str, Any]] = []
        changed = False
        with self._lock:
            for event in self._events:
                if event.get("status") != "scheduled":
                    continue
                state = str(event.get("reminder_state") or "pending")
                if state not in {"pending", "queued_after_recording"}:
                    continue
                if int(event.get("reminder_attempts") or 0) >= 2:
                    event["reminder_state"] = "fallback_required"
                    changed = True
                    continue
                snoozed = event.get("snoozed_until")
                remind_at = parse_datetime(str(snoozed)) if snoozed else (
                    parse_datetime(str(event["start"]))
                    - timedelta(minutes=int(event.get("reminder_minutes") or 0))
                )
                event_end = parse_datetime(str(event["end"]))
                if now < remind_at or now > event_end + timedelta(minutes=5):
                    continue
                if recording_active:
                    if state != "queued_after_recording":
                        event["reminder_state"] = "queued_after_recording"
                        changed = True
                    continue
                event["reminder_state"] = "delivering"
                event["reminder_attempts"] = int(event.get("reminder_attempts") or 0) + 1
                event["last_reminder_at"] = _iso(now)
                due.append(dict(event))
                changed = True
            if changed:
                self._save_events()
        return due

    def mark_reminder_result(self, event_id: str, *, delivered: bool, channel: str, reason: str = "") -> None:
        with self._lock:
            event = next((item for item in self._events if item.get("id") == event_id), None)
            if event is None:
                return
            event["reminder_state"] = "delivered" if delivered else "fallback_required"
            event["reminder_channel"] = channel
            event["reminder_reason"] = reason[:500]
            self._save_events()
        self.audit(
            "agenda.reminder_result",
            {"id": event_id, "delivered": delivered, "channel": channel, "reason": reason[:200]},
        )

    def begin_meeting(self, title: str, consent_statement: str) -> dict[str, Any]:
        now = _now()
        meeting = {
            "id": str(uuid.uuid4()),
            "title": title.strip()[:160] or "未命名会议",
            "consent_statement": consent_statement.strip()[:500],
            "state": "starting",
            "started_at": _iso(now),
            "marks": [],
            "transcript_state": "awaiting_recording",
            "evaluation_state": "awaiting_transcript",
        }
        with self._lock:
            self._meetings.append(meeting)
            self._save_meetings()
        self.audit("meeting.created", {"id": meeting["id"], "title": meeting["title"]})
        return dict(meeting)

    def update_meeting(self, meeting_id: str, **changes: object) -> dict[str, Any]:
        with self._lock:
            meeting = next((item for item in self._meetings if item.get("id") == meeting_id), None)
            if meeting is None:
                raise KeyError(meeting_id)
            meeting.update(changes)
            meeting["updated_at"] = _iso(_now())
            self._save_meetings()
            return dict(meeting)

    def mark_meeting(self, meeting_id: str, label: str, note: str, elapsed_seconds: float) -> dict[str, Any]:
        with self._lock:
            meeting = next((item for item in self._meetings if item.get("id") == meeting_id), None)
            if meeting is None:
                raise KeyError(meeting_id)
            mark = {
                "id": str(uuid.uuid4()),
                "label": label.strip()[:80] or "重点",
                "note": note.strip()[:500],
                "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
                "created_at": _iso(_now()),
            }
            meeting.setdefault("marks", []).append(mark)
            self._save_meetings()
        self.audit("meeting.marked", {"meeting_id": meeting_id, "label": mark["label"]})
        return mark

    def meeting(self, meeting_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = next((item for item in self._meetings if item.get("id") == meeting_id), None)
            return dict(value) if value else None

    def meetings(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in reversed(self._meetings[-20:])]

    def sync_macos_calendar(self, *, hours: int = 48) -> list[dict[str, Any]]:
        """Import Calendar.app events. iCloud/phone calendars synced to macOS are included."""

        hours = min(24 * 30, max(1, int(hours)))
        start_ms = int(_now().timestamp() * 1000)
        end_ms = int((_now() + timedelta(hours=hours)).timestamp() * 1000)
        script = f"""
const app = Application('Calendar');
const start = new Date({start_ms});
const end = new Date({end_ms});
const rows = [];
for (const calendar of app.calendars()) {{
  const calendarName = String(calendar.name());
  for (const event of calendar.events()) {{
    const eventStart = event.startDate();
    if (!(eventStart instanceof Date) || eventStart < start || eventStart > end) continue;
    const eventEnd = event.endDate();
    rows.push({{
      external_event_id: String(event.uid()), calendar_id: calendarName,
      title: String(event.summary() || '未命名日程'),
      start: eventStart.toISOString(), end: eventEnd.toISOString(),
      description: String(event.description() || ''), write_back: false
    }});
  }}
}}
JSON.stringify(rows);
"""
        completed = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or "Calendar.app access failed"
            raise RuntimeError(message[:500])
        try:
            rows = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Calendar.app returned invalid data") from error
        if not isinstance(rows, list):
            raise RuntimeError("Calendar.app returned an invalid event list")
        return self.import_events("macos", [item for item in rows if isinstance(item, dict)])
