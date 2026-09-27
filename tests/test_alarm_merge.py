import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def alarm(ts, external_id, *, source_type="scada", pipeline="P-1", segment="S-8",
          pressure=30.0, ppm=120.0, odor=2, reporter="dispatch-1"):
    return {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": ts,
        "pipeline_id": pipeline,
        "segment_id": segment,
        "pressure_drop_kpa": pressure,
        "sensor_value_ppm": ppm,
        "odor_reports": odor,
        "reporter": reporter,
    }


class AlarmIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_same_segment_within_30_minutes_merges(self):
        first, meta1 = self.service.ingest_alarm(
            alarm("2026-09-27T08:00:00+00:00", "A-1"), "d", "dispatcher")
        second, meta2 = self.service.ingest_alarm(
            alarm("2026-09-27T08:20:00+00:00", "A-2", source_type="patrol"), "d", "dispatcher")
        self.assertFalse(meta1["merged"])
        self.assertTrue(meta2["merged"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(second["sources"]), 2)
        self.assertEqual(self.service.state()["counts"].get("reported", 0), 1)

    def test_same_segment_after_30_minutes_creates_new_event(self):
        first, _ = self.service.ingest_alarm(
            alarm("2026-09-27T08:00:00+00:00", "A-1"), "d", "dispatcher")
        second, meta = self.service.ingest_alarm(
            alarm("2026-09-27T08:30:01+00:00", "A-2"), "d", "dispatcher")
        self.assertFalse(meta["merged"])
        self.assertNotEqual(first["id"], second["id"])

    def test_different_segment_does_not_merge(self):
        self.service.ingest_alarm(
            alarm("2026-09-27T08:00:00+00:00", "A-1", segment="S-1"), "d", "dispatcher")
        second, meta = self.service.ingest_alarm(
            alarm("2026-09-27T08:05:00+00:00", "A-2", segment="S-2"), "d", "dispatcher")
        self.assertFalse(meta["merged"])

    def test_duplicate_external_id_rejected_with_item_pointer(self):
        first, _ = self.service.ingest_alarm(
            alarm("2026-09-27T08:00:00+00:00", "A-1"), "d", "dispatcher")
        with self.assertRaises(ConflictError) as context:
            self.service.ingest_alarm(
                alarm("2026-09-27T08:10:00+00:00", "A-1"), "d", "dispatcher")
        self.assertEqual(context.exception.code, "duplicate_source")
        self.assertEqual(context.exception.details["item_id"], first["id"])
        self.assertIn("已经登记", str(context.exception))


class SourceConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item, _ = self.service.ingest_alarm(
            alarm("2026-09-27T08:00:00+00:00", "A-1", pressure=30.0, ppm=200.0, odor=2),
            "d", "dispatcher")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _contradicting(self, **overrides):
        payload = {
            "source_type": "sensor",
            "external_id": "C-1",
            "observed_at": "2026-09-27T08:10:00+00:00",
            "sensor_value_ppm": 0,
            "pressure_drop_kpa": 0,
            "odor_reports": 0,
        }
        payload.update(overrides)
        return payload

    def test_conflicting_source_holds_event_pending_review(self):
        item = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        self.assertEqual(item["status"], "pending_review")
        conflicts = item["payload"]["pending_review"]["conflicts"]
        dimensions = {conflict["dimension"] for conflict in conflicts}
        self.assertEqual(dimensions, {"odor", "pressure_drop", "concentration"})

    def test_isolate_and_repair_blocked_with_conflict_sources(self):
        item = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                             "sup", "supervisor", item["version"])
        self.assertEqual(context.exception.code, "pending_review")
        self.assertEqual(context.exception.status, 409)
        details = context.exception.details
        self.assertIn("conflicts", details)
        # 返回里要能看到冲突双方是谁
        conflict = details["conflicts"][0]
        self.assertTrue(conflict["positive_sources"])
        self.assertTrue(conflict["negative_sources"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "repair", {"work_order": "WO-1"},
                             "tech", "technician", item["version"])
        self.assertEqual(context.exception.code, "pending_review")

    def test_reviewer_conclusion_resumes_and_clears_block(self):
        item = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        self.assertEqual(item["status"], "pending_review")
        # 普通调度/巡检不能复核
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "review",
                             {"decision": "proceed", "conclusion": "现场确认有泄漏"},
                             "d", "dispatcher", item["version"])
        self.assertEqual(context.exception.status, 403)
        # 复核结论是必填
        with self.assertRaises(DomainError):
            self.service.act(item["id"], "review", {"decision": "proceed"},
                             "rev", "reviewer", item["version"])
        resumed = self.service.act(
            item["id"], "review",
            {"decision": "proceed", "conclusion": "现场复核确认有泄漏，传感零值为设备故障"},
            "rev", "reviewer", item["version"])
        self.assertEqual(resumed["status"], "reported")
        self.assertNotIn("pending_review", resumed["payload"])
        # 恢复后可以继续核验、隔离
        resumed = self.service.act(resumed["id"], "verify", {"field_confirmed": True},
                                   "r", "responder", resumed["version"])
        isolated = self.service.act(resumed["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                                    "sup", "supervisor", resumed["version"])
        self.assertEqual(isolated["status"], "isolated")

    def test_reviewer_can_cancel_as_false_alarm(self):
        item = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        cancelled = self.service.act(
            item["id"], "review",
            {"decision": "cancel", "conclusion": "现场无异味无压降，初报为误报"},
            "rev", "reviewer", item["version"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["payload"]["cancellation"]["via"], "review")

    def test_non_conflicting_source_does_not_hold(self):
        item = self.service.add_source(self.item["id"], {
            "source_type": "patrol",
            "external_id": "C-2",
            "observed_at": "2026-09-27T08:10:00+00:00",
            "sensor_value_ppm": 180,
            "pressure_drop_kpa": 28,
            "odor_reports": 3,
        }, "p", "patrol")
        self.assertEqual(item["status"], "reported")
        self.assertIsNone(item["payload"].get("pending_review"))

    def test_merge_with_conflicting_alarm_holds_existing_event(self):
        base, _ = self.service.ingest_alarm(
            alarm("2026-09-27T09:00:00+00:00", "B-1", pressure=30.0, ppm=200.0, odor=2),
            "d", "dispatcher")
        merged, meta = self.service.ingest_alarm(
            alarm("2026-09-27T09:15:00+00:00", "B-9", source_type="sensor",
                  pressure=0.0, ppm=0.0, odor=0), "sensor-gw", "sensor")
        self.assertTrue(meta["merged"])
        self.assertEqual(merged["id"], base["id"])
        self.assertEqual(merged["status"], "pending_review")
        self.assertIsNotNone(meta["held"])

    def test_next_step_guides_dispatcher(self):
        guided = self.service.get_item(self.item["id"])
        self.assertEqual(guided["next_step"]["action"], "verify")
        held = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        self.assertEqual(held["next_step"]["action"], "review")
        self.assertIn("conclusion", held["next_step"]["missing"])

    def test_dismissed_conflict_does_not_retrigger_and_summary_dedups(self):
        # 告警建事件时初报就是第一条来源，摘要不应重复显示
        detail = self.service.get_item(self.item["id"])
        labels = [row["label"] for row in detail["source_summary"]]
        self.assertEqual(len(labels), 1)
        self.assertTrue(labels[0].startswith("初报"))

        held = self.service.add_source(self.item["id"], self._contradicting(), "p", "patrol")
        resumed = self.service.act(
            held["id"], "review",
            {"decision": "proceed", "conclusion": "传感器故障，采信初报"},
            "rev", "reviewer", held["version"])
        self.assertEqual(resumed["status"], "reported")
        # 被复核驳回的那条来源（C-1）信号不再参与冲突判定；
        # 但一条全新的对立来源（C-9）必须重新挂起，提醒值班员可能有新情况
        again = self.service.add_source(resumed["id"], {
            "source_type": "sensor",
            "external_id": "C-9",
            "observed_at": "2026-09-27T09:00:00+00:00",
            "pressure_drop_kpa": 0,
            "sensor_value_ppm": 0,
            "odor_reports": 0,
        }, "p", "patrol")
        self.assertEqual(again["status"], "pending_review")
        reheld = self.service.get_item(resumed["id"])
        # 新一轮冲突里只看到 C-9，看不到已驳回的 C-1
        negatives = {
            (s["source_type"], s["external_id"])
            for c in reheld["payload"]["pending_review"]["conflicts"]
            for s in c["negative_sources"]
        }
        self.assertIn(("sensor", "C-9"), negatives)
        self.assertNotIn(("sensor", "C-1"), negatives)


if __name__ == "__main__":
    unittest.main()
