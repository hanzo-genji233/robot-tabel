from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from assistant import AssistantStore
from service import MediaLabService


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class AssistantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = AssistantStore(Path(self.temporary.name))
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def event(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "source": "local",
            "calendar_id": "smart-assistant",
            "title": "产品评审",
            "start": iso(self.now + timedelta(minutes=5)),
            "end": iso(self.now + timedelta(minutes=65)),
            "reminder_minutes": 15,
        }
        value.update(overrides)
        return self.store.upsert_event(value)

    def test_imported_source_is_read_only_by_default(self) -> None:
        imported = self.store.import_events("feishu", [{
            "external_event_id": "fs-1",
            "calendar_id": "primary",
            "title": "产品评审",
            "start": iso(self.now + timedelta(minutes=5)),
            "end": iso(self.now + timedelta(minutes=65)),
        }])[0]
        self.assertEqual(imported["source"], "feishu")
        self.assertFalse(imported["write_back"])

    def test_cross_source_duplicates_are_marked_but_retained(self) -> None:
        first = self.event(source="local", external_event_id="local-1")
        second = self.event(source="feishu", external_event_id="feishu-1")
        events = self.store.list_events(self.now, self.now + timedelta(hours=2))
        self.assertEqual({first["id"], second["id"]}, {item["id"] for item in events})
        self.assertTrue(all(item["possible_duplicate"] for item in events))

    def test_recording_queues_reminder_until_recording_ends(self) -> None:
        event = self.event()
        self.assertEqual(self.store.claim_due_reminders(recording_active=True), [])
        self.assertEqual(self.store.event(str(event["id"]))["reminder_state"], "queued_after_recording")
        due = self.store.claim_due_reminders(recording_active=False)
        self.assertEqual([event["id"]], [item["id"] for item in due])

    def test_reminder_attempts_stop_after_two(self) -> None:
        event = self.event(reminder_attempts=2)
        self.assertEqual(self.store.claim_due_reminders(recording_active=False), [])
        self.assertEqual(self.store.event(str(event["id"]))["reminder_state"], "fallback_required")

    def test_todo_mark_is_only_a_timestamp_anchor(self) -> None:
        meeting = self.store.begin_meeting("产品周会", "已获得录音同意")
        self.store.mark_meeting(
            str(meeting["id"]),
            "待办",
            "",
            32.5,
        )
        updated = self.store.meeting(str(meeting["id"]))
        self.assertEqual(updated["action_items"], [])

    def test_transcript_action_item_can_be_confirmed_without_external_write(self) -> None:
        meeting = self.store.begin_meeting("产品周会", "已获得录音同意")
        self.store.mark_meeting(str(meeting["id"]), "待办", "同步项目组", 12)
        updated = self.store.replace_action_items(str(meeting["id"]), [{
            "owner": "小王", "title": "同步项目组", "due": "周五",
        }])
        item = updated["action_items"][0]
        confirmed = self.store.update_action_item(
            str(meeting["id"]), str(item["id"]), "confirm"
        )
        self.assertEqual(confirmed["state"], "confirmed_local")
        self.assertFalse(confirmed["write_back"])


class MeetingPipelineTests(unittest.TestCase):
    def test_extracts_real_action_items_from_summary_table(self) -> None:
        summary = """## 待办事项
| 责任人 | 任务 | 截止时间 |
|---|---|---|
| 小王 | 下载标注文件 | 周五 |
| 待确认 | 交由 AI 分析评分与证书 | 待确认 |

## 存疑点
"""
        items = MediaLabService._extract_summary_action_items(summary)
        self.assertEqual([item["title"] for item in items], [
            "下载标注文件", "交由 AI 分析评分与证书",
        ])

    def test_run_meeting_tool_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python"
            script = root / "transcribe.py"
            python.touch()
            script.touch()
            completed = type("Completed", (), {"returncode": 0, "stderr": "", "stdout": ""})()
            with (
                patch("service._MEETING_PIPELINE_PYTHON", python),
                patch("service.subprocess.run", return_value=completed) as run,
            ):
                MediaLabService._run_meeting_tool(script, ["meeting.wav"], timeout=12)
            run.assert_called_once_with(
                [str(python), str(script), "meeting.wav"],
                check=False,
                capture_output=True,
                text=True,
                timeout=12,
            )

    def test_pipeline_status_reports_missing_component(self) -> None:
        service = MediaLabService.__new__(MediaLabService)
        with patch("service._MEETING_PIPELINE_PYTHON", Path("/missing/python")):
            status = service._meeting_pipeline_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "extension_required")
        self.assertIn("/missing/python", status["missing"])

    def test_run_meeting_tool_surfaces_script_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "python"
            script = root / "summarize.py"
            python.touch()
            script.touch()
            completed = type("Completed", (), {"returncode": 2, "stderr": "summary failed", "stdout": ""})()
            with (
                patch("service._MEETING_PIPELINE_PYTHON", python),
                patch("service.subprocess.run", return_value=completed),
            ):
                with self.assertRaisesRegex(RuntimeError, "summarize.py failed: summary failed"):
                    MediaLabService._run_meeting_tool(script, [], timeout=1)

    def test_meeting_artifact_path_rejects_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = MediaLabService.__new__(MediaLabService)
            service._assistant = AssistantStore(Path(directory))
            meeting_id = "11111111-1111-1111-1111-111111111111"
            output_dir = service._assistant.meetings_dir / meeting_id
            output_dir.mkdir()
            artifact = output_dir / "minutes.md"
            artifact.write_text("ok", encoding="utf-8")
            self.assertEqual(service.assistant_meeting_artifact_path(meeting_id, artifact.name), artifact)
            self.assertIsNone(service.assistant_meeting_artifact_path(meeting_id, "../minutes.md"))
            self.assertIsNone(service.assistant_meeting_artifact_path("not-a-uuid", artifact.name))
            self.assertIsNone(service.assistant_meeting_artifact_path(meeting_id, "minutes.txt"))


if __name__ == "__main__":
    unittest.main()
