from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assistant import AssistantStore


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


if __name__ == "__main__":
    unittest.main()
