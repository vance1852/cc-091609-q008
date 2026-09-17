"""调剂防错服务单元测试：python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from dispensing import (
    DispensingService,
    InvalidStateError,
    LineStatus,
    LockReason,
    PrescriptionLine,
    ScaleAction,
    ScaleEvent,
    SelfReviewError,
    TaskStatus,
)

T0 = datetime(2026, 9, 17, 8, 0, 0)


def at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def lines() -> list[PrescriptionLine]:
    return [
        PrescriptionLine("line-a", "rx-88-v2", "zhinanxing", "制南星", Decimal("9.0"), Decimal("0.5")),
        PrescriptionLine("line-b", "rx-88-v2", "dannanxing", "胆南星", Decimal("6.0"), Decimal("0.5")),
    ]


BINS = {"line-a": "bin-141", "line-b": "bin-142"}


def ev(event_id: str, line: str, bin_code: str, action: ScaleAction, reading: str,
       device: str = "scale-5", minute: int = 1, calibration: bool = True) -> ScaleEvent:
    return ScaleEvent(
        event_id=event_id,
        device_id=device,
        line_id=line,
        bin_code=bin_code,
        action=action,
        reading_grams=Decimal(reading),
        occurred_at=at(minute),
        calibration_valid=calibration,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = DispensingService()
        self.svc.claim_task("t-1", "disp-01", lines(), BINS, at=at(0))

    def scan(self, line: str, bin_code: str, scan_id: str = "sc-1"):
        return self.svc.scan_bin("t-1", line, bin_code, scan_id=scan_id, at=at(1), operator_id="disp-01")

    def activate(self, line: str = "line-a", bin_code: str = "bin-141") -> None:
        self.scan(line, bin_code)

    def weigh_line_a_ok(self) -> None:
        """line-a 在 scale-5 上完成合格称量。"""
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.0"))
        self.svc.record_scale_event("t-1", ev("e3", "line-a", "bin-141", ScaleAction.ACCEPT, "9.0"))

    def weigh_line_b_ok(self, prefix: str = "f") -> None:
        self.scan("line-b", "bin-142", scan_id="sc-2")
        self.svc.record_scale_event("t-1", ev(f"{prefix}1", "line-b", "bin-142", ScaleAction.TARE, "1.5"))
        self.svc.record_scale_event("t-1", ev(f"{prefix}2", "line-b", "bin-142", ScaleAction.ADD, "6.0"))
        self.svc.record_scale_event("t-1", ev(f"{prefix}3", "line-b", "bin-142", ScaleAction.ACCEPT, "6.0"))

    def weigh_all_and_seal(self) -> None:
        self.weigh_line_a_ok()
        self.weigh_line_b_ok()
        self.svc.seal_package("t-1", seal="seal-1", at=at(30), operator_id="disp-01")


class ScanTests(Base):
    def test_wrong_bin_locks_identity_mismatch(self):
        outcome = self.scan("line-a", "bin-142")  # 制南星扫到胆南星的斗
        self.assertFalse(outcome.matched)
        self.assertEqual(outcome.lock.reason, LockReason.IDENTITY_MISMATCH)
        self.assertEqual(self.svc.task("t-1").lines["line-a"].status, LineStatus.LOCKED)

    def test_disabled_bin_locks_line(self):
        self.svc.disable_bin("bin-141")
        outcome = self.scan("line-a", "bin-141")
        self.assertFalse(outcome.matched)
        self.assertEqual(outcome.lock.reason, LockReason.BIN_DISABLED)

    def test_scan_is_idempotent(self):
        first = self.scan("line-a", "bin-142")
        second = self.svc.scan_bin("t-1", "line-a", "bin-142", scan_id="sc-1", at=at(2))
        self.assertIs(first, second)
        self.assertEqual(len(self.svc.task("t-1").lock_history), 1)

    def test_cannot_scan_while_other_line_active(self):
        self.activate()
        with self.assertRaises(InvalidStateError):
            self.scan("line-b", "bin-142", scan_id="sc-2")


class ScaleEventTests(Base):
    def test_reading_only_goes_to_current_line(self):
        self.activate("line-a", "bin-141")
        outcome = self.svc.record_scale_event("t-1", ev("e1", "line-b", "bin-142", ScaleAction.TARE, "1.5"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "not-current-line")
        self.assertEqual(self.svc.task("t-1").lines["line-b"].tare_grams, None)

    def test_calibration_expired_locks_and_skips_reading(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        outcome = self.svc.record_scale_event(
            "t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.6", calibration=False)
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.lock.reason, LockReason.CALIBRATION_EXPIRED)
        self.assertEqual(self.svc.task("t-1").lines["line-a"].net_grams, Decimal("0"))

    def test_out_of_tolerance_locks_on_accept(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "10.0"))
        outcome = self.svc.record_scale_event("t-1", ev("e3", "line-a", "bin-141", ScaleAction.ACCEPT, "10.0"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.lock.reason, LockReason.OUT_OF_TOLERANCE)

    def test_events_on_locked_line_are_rejected(self):
        self.scan("line-a", "bin-142")  # 锁定
        outcome = self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "line-locked")

    def test_event_replay_is_idempotent(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        first = self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.0"))
        second = self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.0"))
        self.assertIs(first, second)
        self.assertEqual(self.svc.task("t-1").lines["line-a"].net_grams, Decimal("9.0"))

    def test_switch_scale_invalidates_old_tare(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0", device="scale-2"))
        self.svc.switch_scale("t-1", "line-a", "scale-5", at=at(5), operator_id="disp-01")
        outcome = self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.0"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "tare-required")

    def test_events_from_old_device_rejected_after_switch(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0", device="scale-2"))
        self.svc.switch_scale("t-1", "line-a", "scale-5", at=at(5))
        outcome = self.svc.record_scale_event(
            "t-1", ev("e2", "line-a", "bin-141", ScaleAction.TARE, "2.0", device="scale-2")
        )
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "device-mismatch")

    def test_accept_reading_must_match_accumulated_net(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.0"))
        outcome = self.svc.record_scale_event("t-1", ev("e3", "line-a", "bin-141", ScaleAction.ACCEPT, "8.8"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "reading-mismatch")

    def test_remove_beyond_net_rejected(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "3.0"))
        outcome = self.svc.record_scale_event("t-1", ev("e3", "line-a", "bin-141", ScaleAction.REMOVE, "4.0"))
        self.assertFalse(outcome.applied)
        self.assertEqual(outcome.note, "remove-exceeds-net")

    def test_all_events_kept_in_order_including_rejected(self):
        self.activate()
        self.svc.record_scale_event("t-1", ev("e1", "line-a", "bin-141", ScaleAction.TARE, "2.0"))
        self.svc.record_scale_event("t-1", ev("e2", "line-a", "bin-141", ScaleAction.ADD, "9.6"))
        self.svc.record_scale_event("t-1", ev("e3", "line-a", "bin-141", ScaleAction.REMOVE, "0.6"))
        self.svc.record_scale_event("t-1", ev("e4", "line-a", "bin-141", ScaleAction.ACCEPT, "9.0"))
        log = self.svc.task("t-1").event_log
        self.assertEqual([e.event.event_id for e in log], ["e1", "e2", "e3", "e4"])
        self.assertTrue(all(e.applied for e in log))
        self.assertEqual([e.seq for e in log], [0, 1, 2, 3])


class UnlockTests(Base):
    def test_unlock_resets_line_and_keeps_history(self):
        self.scan("line-a", "bin-142")
        record = self.svc.unlock_line("t-1", "line-a", supervisor_id="lead-01", note="重新扫码", at=at(5))
        line = self.svc.task("t-1").lines["line-a"]
        self.assertEqual(line.status, LineStatus.PENDING)
        self.assertIsNone(line.tare_grams)
        self.assertTrue(record.resolved)
        history = self.svc.handover_report("t-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].reason, LockReason.IDENTITY_MISMATCH)
        self.assertEqual(history[0].unlocked_by, "lead-01")

    def test_handover_report_covers_all_tasks(self):
        self.scan("line-a", "bin-142")
        self.svc.claim_task("t-2", "disp-02", lines(), BINS, at=at(0))
        self.svc.disable_bin("bin-141")
        self.svc.scan_bin("t-2", "line-a", "bin-141", scan_id="sc-9", at=at(2))
        entries = self.svc.handover_report()
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.task_id for e in entries}, {"t-1", "t-2"})


class ReviewTests(Base):
    def test_seal_requires_all_lines_accepted(self):
        self.weigh_line_a_ok()
        with self.assertRaises(InvalidStateError):
            self.svc.seal_package("t-1", seal="seal-1", at=at(30))

    def test_seal_is_idempotent_for_same_seal(self):
        self.weigh_all_and_seal()
        again = self.svc.seal_package("t-1", seal="seal-1", at=at(31))
        self.assertEqual(again.status, TaskStatus.PACKAGED)
        with self.assertRaises(InvalidStateError):
            self.svc.seal_package("t-1", seal="seal-2", at=at(32))

    def test_reviewer_cannot_be_dispenser(self):
        self.weigh_all_and_seal()
        with self.assertRaises(SelfReviewError):
            self.svc.submit_review("t-1", "rv-1", "disp-01", approve=True, at=at(40))

    def test_review_approve_completes_and_slip_is_single(self):
        self.weigh_all_and_seal()
        self.svc.submit_review("t-1", "rv-1", "rev-02", approve=True, at=at(40))
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.COMPLETED)
        slip1 = self.svc.qualified_slip("t-1")
        slip2 = self.svc.qualified_slip("t-1")
        self.assertIs(slip1, slip2)
        self.assertEqual(len(self.svc.qualified_slips()), 1)
        self.assertEqual(slip1.dispenser_id, "disp-01")
        self.assertEqual(slip1.reviewer_id, "rev-02")
        by_line = {item.line_id: item for item in slip1.lines}
        self.assertEqual(by_line["line-a"].target_grams, Decimal("9.0"))
        self.assertEqual(by_line["line-a"].actual_grams, Decimal("9.0"))
        self.assertEqual(by_line["line-a"].device_id, "scale-5")
        self.assertTrue(by_line["line-a"].calibration_valid)

    def test_review_reject_returns_task(self):
        self.weigh_all_and_seal()
        self.svc.submit_review("t-1", "rv-1", "rev-02", approve=False, at=at(40))
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.RETURNED)

    def test_review_is_idempotent(self):
        self.weigh_all_and_seal()
        first = self.svc.submit_review("t-1", "rv-1", "rev-02", approve=True, at=at(40))
        second = self.svc.submit_review("t-1", "rv-1", "rev-02", approve=True, at=at(41))
        self.assertIs(first, second)
        self.assertEqual(len(self.svc.task("t-1").reviews), 1)

    def test_slip_requires_completed(self):
        with self.assertRaises(InvalidStateError):
            self.svc.qualified_slip("t-1")


class PrescriptionChangeTests(Base):
    def test_open_task_voided_on_change(self):
        affected = self.svc.apply_prescription_change("rx-88-v2", "rx-88-v3", at=at(50))
        self.assertEqual(affected, ["t-1"])
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.VOIDED)
        with self.assertRaises(InvalidStateError):
            self.scan("line-a", "bin-141", scan_id="sc-2")

    def test_sealed_task_goes_to_unseal_review_only(self):
        self.weigh_all_and_seal()
        self.svc.apply_prescription_change("rx-88-v2", "rx-88-v3", at=at(50))
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.UNSEAL_REVIEW)
        with self.assertRaises(InvalidStateError):
            self.svc.record_scale_event("t-1", ev("x1", "line-a", "bin-141", ScaleAction.ADD, "0.1"))

    def test_unseal_review_decisions(self):
        self.weigh_all_and_seal()
        self.svc.apply_prescription_change("rx-88-v2", "rx-88-v3", at=at(50))
        with self.assertRaises(SelfReviewError):
            self.svc.complete_unseal_review("t-1", "disp-01", decision="void", at=at(51))
        self.svc.complete_unseal_review("t-1", "rev-02", decision="void", at=at(52))
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.VOIDED)

    def test_unseal_reseal_returns_to_packaged(self):
        self.weigh_all_and_seal()
        self.svc.apply_prescription_change("rx-88-v2", "rx-88-v3", at=at(50))
        self.svc.complete_unseal_review("t-1", "rev-02", decision="reseal", at=at(51), note="误报")
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.PACKAGED)

    def test_completed_task_unaffected(self):
        self.weigh_all_and_seal()
        self.svc.submit_review("t-1", "rv-1", "rev-02", approve=True, at=at(40))
        affected = self.svc.apply_prescription_change("rx-88-v2", "rx-88-v3", at=at(50))
        self.assertEqual(affected, [])
        self.assertEqual(self.svc.task("t-1").status, TaskStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
