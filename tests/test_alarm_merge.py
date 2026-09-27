import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def alarm(segment="S-1", t="2026-09-27T08:00:00+00:00", pressure=30, ppm=120, odor=3, ext=None, pipeline="P-1"):
    payload = {
        "pipeline_id": pipeline,
        "segment_id": segment,
        "reported_at": t,
        "pressure_drop_kpa": pressure,
        "sensor_value_ppm": ppm,
        "odor_reports": odor,
        "reporter": "dispatch-1",
    }
    if ext is not None:
        payload["external_id"] = ext
    return payload


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_same_segment_within_30min_merges(self):
        first = self.service.create_item(alarm(ext="A-1"), "d", "dispatcher")
        second = self.service.create_item(
            alarm(t="2026-09-27T08:20:00+00:00", ext="A-2"), "d", "dispatcher"
        )
        self.assertTrue(second.get("merged"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.service.list_items()), 1)
        self.assertEqual(second["source_summary"]["total"], 2)

    def test_outside_window_creates_new_event(self):
        first = self.service.create_item(alarm(ext="A-1"), "d", "dispatcher")
        second = self.service.create_item(
            alarm(t="2026-09-27T08:40:00+00:00", ext="A-2"), "d", "dispatcher"
        )
        self.assertNotEqual(second["id"], first["id"])
        self.assertFalse(second.get("merged"))
        self.assertEqual(len(self.service.list_items()), 2)

    def test_different_segment_creates_new_event(self):
        first = self.service.create_item(alarm(segment="S-1", ext="A-1"), "d", "dispatcher")
        second = self.service.create_item(alarm(segment="S-2", ext="A-2"), "d", "dispatcher")
        self.assertNotEqual(second["id"], first["id"])

    def test_terminal_event_not_merge_target(self):
        first = self.service.create_item(alarm(ext="A-1"), "d", "dispatcher")
        first = self.service.act(first["id"], "verify", {"field_confirmed": True}, "r", "responder", first["version"])
        first = self.service.act(first["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", first["version"])
        first = self.service.act(first["id"], "repair", {"work_order": "WO-1"}, "t", "technician", first["version"])
        first = self.service.act(first["id"], "pressure_test", {"test_passed": True, "pressure_kpa": 150}, "t", "technician", first["version"])
        first = self.service.act(first["id"], "restore", {"hazards_clear": True}, "s", "supervisor", first["version"])
        second = self.service.create_item(
            alarm(t="2026-09-27T08:20:00+00:00", ext="A-2"), "d", "dispatcher"
        )
        self.assertNotEqual(second["id"], first["id"])

    def test_duplicate_external_id_rejected_on_create(self):
        self.service.create_item(alarm(ext="EXT-9"), "d", "dispatcher")
        with self.assertRaises(ConflictError) as context:
            self.service.create_item(alarm(segment="S-2", ext="EXT-9"), "d", "dispatcher")
        self.assertEqual(context.exception.code, "duplicate_external_id")
        self.assertIn("已经登记", str(context.exception))
        self.assertEqual(context.exception.details["external_id"], "EXT-9")

    def test_duplicate_external_id_checked_before_merge(self):
        first = self.service.create_item(alarm(ext="EXT-1"), "d", "dispatcher")
        with self.assertRaises(ConflictError):
            self.service.create_item(
                alarm(t="2026-09-27T08:10:00+00:00", ext="EXT-1"), "d", "dispatcher"
            )
        self.assertEqual(len(self.service.list_items()), 1)


class SourceConflictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            alarm(pressure=30, ppm=120, odor=3, ext="A-1"), "d", "dispatcher"
        )
        self.item = self.service.act(
            self.item["id"], "verify", {"field_confirmed": True}, "r", "responder", self.item["version"]
        )

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _source(self, **overrides):
        payload = {
            "source_type": "patrol",
            "external_id": "P-100",
            "observed_at": "2026-09-27T08:15:00+00:00",
            "pressure_drop_kpa": 30,
            "sensor_value_ppm": 120,
            "odor_reports": 3,
        }
        payload.update(overrides)
        return payload

    def test_zero_concentration_conflict_pauses_event(self):
        result = self.service.add_source(
            self.item["id"],
            self._source(external_id="P-100", sensor_value_ppm=0, pressure_drop_kpa=30, odor_reports=3),
            "patrol-1", "patrol",
        )
        self.assertEqual(result["status"], "pending_verification")
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(result["conflicts"][0]["dimension"], "concentration")
        self.assertEqual(result["next_step"]["action"], "review")
        self.assertEqual(result["next_step"]["pending_count"], 1)

    def test_isolate_blocked_and_returns_conflicting_sources(self):
        self.service.add_source(
            self.item["id"], self._source(external_id="P-100", sensor_value_ppm=0), "p", "patrol"
        )
        fresh = self.service.get_item(self.item["id"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(
                self.item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
                "s", "supervisor", fresh["version"],
            )
        self.assertEqual(context.exception.code, "source_conflict_pending")
        details = context.exception.details
        self.assertEqual(details["action"], "isolate")
        self.assertEqual(details["pending_verifications"][0]["external_id"], "P-100")

    def test_repair_blocked_while_pending(self):
        isolated = self.service.act(
            self.item["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]}, "s", "supervisor", self.item["version"]
        )
        self.service.add_source(
            self.item["id"], self._source(external_id="P-101", odor_reports=0), "p", "patrol"
        )
        fresh = self.service.get_item(self.item["id"])
        with self.assertRaises(ConflictError) as context:
            self.service.act(
                self.item["id"], "repair", {"work_order": "WO-2"}, "t", "technician", fresh["version"]
            )
        self.assertEqual(context.exception.code, "source_conflict_pending")
        self.assertEqual(fresh["status"], "pending_verification")

    def test_reviewer_conclusion_resumes_workflow(self):
        self.service.add_source(
            self.item["id"], self._source(external_id="P-100", sensor_value_ppm=0), "p", "patrol"
        )
        fresh = self.service.get_item(self.item["id"])
        with self.assertRaises(DomainError):
            self.service.act(fresh["id"], "review", {"decision": "confirm_leak"}, "rev", "reviewer", fresh["version"])
        resumed = self.service.act(
            fresh["id"],
            "review",
            {"decision": "confirm_leak", "conclusion": "现场复核确认泄漏，零浓度读数为设备误报"},
            "rev", "reviewer", fresh["version"],
        )
        self.assertEqual(resumed["status"], "verified")
        self.assertNotIn("pending_verifications", resumed["payload"])
        self.assertEqual(resumed["payload"]["reviews"][0]["decision"], "confirm_leak")
        resumed = self.service.act(
            resumed["id"], "isolate", {"valve_sequence": ["V-1", "V-2"]},
            "s", "supervisor", resumed["version"],
        )
        self.assertEqual(resumed["status"], "isolated")

    def test_review_dismiss_cancels_event(self):
        self.service.add_source(
            self.item["id"], self._source(external_id="P-100", sensor_value_ppm=0), "p", "patrol"
        )
        fresh = self.service.get_item(self.item["id"])
        resumed = self.service.act(
            fresh["id"],
            "review",
            {"decision": "dismiss", "conclusion": "确认为误报，压降来自正常工况"},
            "rev", "reviewer", fresh["version"],
        )
        self.assertEqual(resumed["status"], "cancelled")

    def test_sensor_role_cannot_review(self):
        self.service.add_source(
            self.item["id"], self._source(external_id="P-100", sensor_value_ppm=0), "p", "patrol"
        )
        fresh = self.service.get_item(self.item["id"])
        with self.assertRaises(DomainError) as context:
            self.service.act(
                fresh["id"], "review", {"conclusion": "x"}, "sn", "sensor", fresh["version"]
            )
        self.assertEqual(context.exception.status, 403)

    def test_consistent_source_does_not_pause(self):
        result = self.service.add_source(
            self.item["id"], self._source(external_id="P-200", sensor_value_ppm=150), "p", "patrol"
        )
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["next_step"].get("pending_count"))

    def test_missing_dimension_does_not_conflict(self):
        # 只报压降的来源，不该触发浓度/异味冲突
        result = self.service.add_source(
            self.item["id"],
            {
                "source_type": "scada",
                "external_id": "S-9",
                "observed_at": "2026-09-27T08:15:00+00:00",
                "pressure_drop_kpa": 32,
            },
            "sys", "sensor",
        )
        self.assertEqual(result["status"], "verified")

    def test_duplicate_external_id_on_sources(self):
        self.service.add_source(self.item["id"], self._source(external_id="P-300"), "p", "patrol")
        with self.assertRaises(ConflictError) as context:
            self.service.add_source(
                self.item["id"], self._source(external_id="P-300"), "p", "patrol"
            )
        self.assertEqual(context.exception.code, "duplicate_external_id")

    def test_merge_with_conflict_pauses(self):
        # 已核验有浓度，新调度告警报零浓度并在窗口内：并入且挂起
        merged = self.service.create_item(
            alarm(t="2026-09-27T08:20:00+00:00", ppm=0, pressure=30, odor=3, ext="A-2"),
            "d", "dispatcher",
        )
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["status"], "pending_verification")


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_state_lists_items_with_summary_and_next_step(self):
        item = self.service.create_item(alarm(ext="A-1"), "d", "dispatcher")
        state = self.service.state()
        entry = state["items"][0]
        self.assertIn("source_summary", entry)
        self.assertIn("next_step", entry)
        self.assertEqual(entry["next_step"]["action"], "verify")
        self.assertEqual(entry["source_summary"]["by_type"].get("dispatch_alarm"), 1)
        self.assertIsNotNone(item)


if __name__ == "__main__":
    unittest.main()
