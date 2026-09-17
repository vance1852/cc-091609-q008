"""调剂防错服务规则测试。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from dispensing.contracts import (
    AttemptStatus,
    Bin,
    LockReason,
    PrescriptionLine,
    ScaleAction,
    ScaleDevice,
    ScaleEvent,
)
from dispensing.service import (
    BinDisabledError,
    CalibrationExpiredError,
    DispensingService,
    IdentityMismatchError,
    IdempotencyConflictError,
    InvalidatedTaskError,
    InvalidReadingError,
    LineLockedError,
    NotCurrentLineError,
    SealedPackageError,
    SelfReviewError,
    SheetNotReadyError,
    StaleDeviceError,
    StaleTareError,
    ToleranceExceededError,
)

T = datetime(2026, 9, 17, 9, 0, 0)


def make_service(initial_device="scale-1", dispenser="d1"):
    lines = [
        PrescriptionLine(
            line_id="l1", snapshot_id="snap-1", herb_code="h-nanxing-zhi",
            display_name="制南星", target_grams=Decimal("9.0"),
            tolerance_grams=Decimal("0.5"),
        ),
        PrescriptionLine(
            line_id="l2", snapshot_id="snap-1", herb_code="h-nanxing-dan",
            display_name="胆南星", target_grams=Decimal("6.0"),
            tolerance_grams=Decimal("0.3"),
        ),
    ]
    bins = [
        Bin(code="b-141", herb_code="h-nanxing-zhi", display_name="制南星"),
        Bin(code="b-142", herb_code="h-nanxing-dan", display_name="胆南星"),
        Bin(code="b-off", herb_code="h-nanxing-zhi", display_name="制南星", enabled=False),
    ]
    scales = [
        ScaleDevice(device_id="scale-1", calibration_valid=True),
        ScaleDevice(device_id="scale-2", calibration_valid=False),
    ]
    return DispensingService(
        session_id="sess-1", snapshot_id="snap-1", dispenser_id=dispenser,
        lines=lines, bins=bins, scales=scales, initial_device_id=initial_device,
    )


def ev(eid, *, device="scale-1", line="l1", bin_code="b-141", action=ScaleAction.ADD,
       reading="0", calibrated=True, at=T):
    return ScaleEvent(
        event_id=eid, device_id=device, line_id=line, bin_code=bin_code,
        action=action, reading_grams=Decimal(reading), occurred_at=at,
        calibration_valid=calibrated,
    )


# -- 身份核验 ---------------------------------------------------------------

def test_wrong_bin_locks_line_immediately():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(IdentityMismatchError) as exc:
        svc.scan_bin("l1", "scan-1", "b-142", T)
    assert exc.value.lock.reason == LockReason.IDENTITY_MISMATCH
    # 锁定后任何读数一律拒绝
    with pytest.raises(LineLockedError):
        svc.record_event(ev("e1"))
    # 历史锁定原因保留
    assert svc.locks[0].line_id == "l1"
    assert svc.locks[0].attempt_no == 1
    assert "胆南星" in svc.locks[0].detail


def test_disabled_bin_locks():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(BinDisabledError):
        svc.scan_bin("l1", "scan-1", "b-off", T)


def test_scan_required_before_weighing():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(IdentityMismatchError):
        svc.record_event(ev("e1"))


def test_event_bin_must_match_verified_bin():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    with pytest.raises(IdentityMismatchError):
        svc.record_event(ev("e1", bin_code="b-142"))


# -- 读数归属 ---------------------------------------------------------------

def test_reading_only_belongs_to_current_line():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(NotCurrentLineError):
        svc.record_event(ev("e1", line="l2"))


def test_only_one_line_active_at_a_time():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(Exception):
        svc.begin_line("l2")


# -- 校准 -------------------------------------------------------------------

def test_calibration_expired_locks_on_tare():
    svc = make_service(initial_device="scale-2")
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    with pytest.raises(CalibrationExpiredError) as exc:
        svc.record_event(ev("e1", device="scale-2", calibrated=False,
                            action=ScaleAction.TARE, reading="0"))
    assert exc.value.lock.reason == LockReason.CALIBRATION_EXPIRED
    # 校准状态随事件快照留痕
    assert svc.lines["l1"].attempt.events[0].calibration_valid is False


def test_calibration_expiring_mid_session_locks_next_reading():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.set_calibration("scale-1", valid=False)
    with pytest.raises(CalibrationExpiredError):
        svc.record_event(ev("e2", action=ScaleAction.ADD, reading="109", calibrated=False))


# -- 换秤与去皮 -------------------------------------------------------------

def test_stale_device_reading_rejected_after_switch():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    # scale-2 改为有效后换秤
    svc.set_calibration("scale-2", valid=True)
    svc.switch_scale("scale-2", T)
    attempt = svc.lines["l1"].attempt
    assert attempt.active_tare_device is None
    assert attempt.net_grams == Decimal("0")
    # 旧秤读数拒收
    with pytest.raises(StaleDeviceError):
        svc.record_event(ev("e2", device="scale-1", action=ScaleAction.ADD, reading="109"))
    # 新秤未去皮，旧去皮值不得沿用
    with pytest.raises(StaleTareError):
        svc.record_event(ev("e3", device="scale-2", action=ScaleAction.ADD, reading="109"))
    # 重新去皮后正常
    svc.record_event(ev("e4", device="scale-2", action=ScaleAction.TARE, reading="110"))
    svc.record_event(ev("e5", device="scale-2", action=ScaleAction.ADD, reading="119"))
    assert svc.lines["l1"].attempt.net_grams == Decimal("9")


def test_switch_to_expired_scale_rejected():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(CalibrationExpiredError):
        svc.switch_scale("scale-2", T)


# -- 加减回退与容差 ---------------------------------------------------------

def test_add_remove_events_kept_and_remove_allows_correction():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e2", action=ScaleAction.ADD, reading="111"))     # +11 加多
    svc.record_event(ev("e3", action=ScaleAction.REMOVE, reading="109"))  # 回退到 9
    attempt = svc.lines["l1"].attempt
    assert [e.event_id for e in attempt.events] == ["e1", "e2", "e3"]
    assert attempt.net_grams == Decimal("9")
    svc.record_event(ev("e4", action=ScaleAction.ACCEPT, reading="109"))
    assert svc.lines["l1"].actual_grams == Decimal("9")


def test_accept_out_of_tolerance_locks():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e2", action=ScaleAction.ADD, reading="110"))  # 净重 10 > 9.5
    with pytest.raises(ToleranceExceededError) as exc:
        svc.record_event(ev("e3", action=ScaleAction.ACCEPT, reading="110"))
    assert exc.value.lock.reason == LockReason.TOLERANCE_EXCEEDED


def test_accept_below_tolerance_locks():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e2", action=ScaleAction.ADD, reading="108"))  # 净重 8 < 8.5
    with pytest.raises(ToleranceExceededError):
        svc.record_event(ev("e3", action=ScaleAction.ACCEPT, reading="108"))


def test_negative_net_reading_rejected():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    with pytest.raises(InvalidReadingError):
        svc.record_event(ev("e2", action=ScaleAction.ADD, reading="99"))


# -- 重试与锁定留痕 ---------------------------------------------------------

def test_retry_keeps_lock_history():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(IdentityMismatchError):
        svc.scan_bin("l1", "scan-1", "b-142", T)
    svc.retry_line("l1")
    assert svc.lines["l1"].attempt_no == 2
    assert svc.lines["l1"].attempts[1].status == AttemptStatus.LOCKED
    assert svc.lines["l1"].attempts[2].status == AttemptStatus.ACTIVE
    # 新尝试仍须重新扫码去皮
    with pytest.raises(IdentityMismatchError):
        svc.record_event(ev("e1"))


def test_retry_without_lock_rejected():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(Exception):
        svc.retry_line("l1")


# -- 幂等 -------------------------------------------------------------------

def test_scan_idempotent():
    svc = make_service()
    svc.begin_line("l1")
    r1 = svc.scan_bin("l1", "scan-1", "b-141", T)
    r2 = svc.scan_bin("l1", "scan-1", "b-141", T)
    assert r1 is r2
    assert len(svc.lines["l1"].attempt.scans) == 1


def test_event_idempotent_resend_does_not_double_apply():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    add = ev("e2", action=ScaleAction.ADD, reading="109")
    svc.record_event(add)
    svc.record_event(add)  # 秤台重发
    assert len(svc.lines["l1"].attempt.events) == 2
    assert svc.lines["l1"].attempt.net_grams == Decimal("9")


def test_event_replay_with_conflicting_payload_rejected():
    svc = make_service()
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    with pytest.raises(IdempotencyConflictError):
        svc.record_event(ev("e1", action=ScaleAction.TARE, reading="120"))


def test_review_idempotent():
    svc = make_service()
    _weigh_l1(svc)
    r1 = svc.approve_line("l1", "r2", "rev-1", T)
    r2 = svc.approve_line("l1", "r2", "rev-1", T)
    assert r1 is r2
    assert len(svc.lines["l1"].reviews) == 1


# -- 双人复核 ---------------------------------------------------------------

def _weigh_l1(svc, reading="109"):
    svc.begin_line("l1")
    svc.scan_bin("l1", "scan-1", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e2", action=ScaleAction.ADD, reading=reading))
    svc.record_event(ev("e3", action=ScaleAction.ACCEPT, reading=reading))


def test_self_review_rejected():
    svc = make_service(dispenser="d1")
    _weigh_l1(svc)
    with pytest.raises(SelfReviewError):
        svc.approve_line("l1", "d1", "rev-1", T)


def test_review_before_weighing_rejected():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(Exception):
        svc.approve_line("l1", "r2", "rev-1", T)


def test_seal_requires_review():
    svc = make_service()
    _weigh_l1(svc)
    with pytest.raises(Exception):
        svc.seal_line("l1", "seal-1", T)


def test_sealed_line_only_allows_unpack_review():
    svc = make_service()
    _weigh_l1(svc)
    svc.approve_line("l1", "r2", "rev-1", T)
    svc.seal_line("l1", "seal-1", T)
    with pytest.raises(SealedPackageError):
        svc.approve_line("l1", "r3", "rev-2", T)
    unpack = svc.approve_line("l1", "r3", "rev-3", T, unpacked=True)
    assert unpack.unpacked is True
    assert unpack.package_seal == "seal-1"


def test_unpack_review_on_unsealed_rejected():
    svc = make_service()
    _weigh_l1(svc)
    with pytest.raises(SealedPackageError):
        svc.approve_line("l1", "r2", "rev-1", T, unpacked=True)


# -- 处方变更 ---------------------------------------------------------------

def test_prescription_change_invalidates_unpacked_only():
    svc = make_service()
    _weigh_l1(svc)
    svc.approve_line("l1", "r2", "rev-1", T)
    svc.seal_line("l1", "seal-1", T)  # l1 已封袋
    svc.begin_line("l2")
    svc.scan_bin("l2", "scan-2", "b-142", T)
    svc.record_event(ev("e4", line="l2", bin_code="b-142",
                        action=ScaleAction.TARE, reading="100"))

    svc.apply_prescription_change(T)

    assert svc.lines["l1"].seal_id == "seal-1"  # 已封袋不受影响
    assert svc.lines["l1"].invalidated is False
    assert svc.lines["l2"].invalidated is True
    assert svc.lines["l2"].attempt.status == AttemptStatus.INVALIDATED
    # 失效任务上的操作被拒
    with pytest.raises(InvalidatedTaskError):
        svc.record_event(ev("e5", line="l2", bin_code="b-142",
                            action=ScaleAction.ADD, reading="106"))
    with pytest.raises(InvalidatedTaskError):
        svc.seal_line("l2", "seal-2", T)


# -- 调剂单 -----------------------------------------------------------------

def test_sheet_blocked_until_all_lines_sealed():
    svc = make_service()
    _weigh_l1(svc)
    with pytest.raises(SheetNotReadyError) as exc:
        svc.generate_dispensing_sheet(T)
    assert "l2" in str(exc.value)


def test_full_qualified_flow_single_sheet():
    svc = make_service()
    _weigh_l1(svc)
    svc.approve_line("l1", "r2", "rev-1", T)
    svc.seal_line("l1", "seal-1", T)

    svc.begin_line("l2")
    svc.scan_bin("l2", "scan-2", "b-142", T)
    svc.record_event(ev("e4", line="l2", bin_code="b-142",
                        action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e5", line="l2", bin_code="b-142",
                        action=ScaleAction.ADD, reading="106"))
    svc.record_event(ev("e6", line="l2", bin_code="b-142",
                        action=ScaleAction.ACCEPT, reading="106"))
    svc.approve_line("l2", "r2", "rev-2", T)
    svc.seal_line("l2", "seal-2", T)

    assert svc.is_qualified()
    sheet = svc.generate_dispensing_sheet(T)
    assert sheet["qualified"] is True
    assert len(sheet["lines"]) == 2
    l1 = sheet["lines"][0]
    assert (l1["display_name"], l1["target_grams"], l1["actual_grams"]) == (
        "制南星", "9.0", "9")
    assert l1["device"] == {"device_id": "scale-1", "calibration_valid": True}
    assert (l1["dispenser_id"], l1["reviewer_id"]) == ("d1", "r2")
    assert l1["package_seal"] == "seal-1"


def test_sheet_reports_lock_history_per_line():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(IdentityMismatchError):
        svc.scan_bin("l1", "scan-1", "b-142", T)
    svc.retry_line("l1")
    svc.scan_bin("l1", "scan-1b", "b-141", T)
    svc.record_event(ev("e1", action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e2", action=ScaleAction.ADD, reading="109"))
    svc.record_event(ev("e3", action=ScaleAction.ACCEPT, reading="109"))
    svc.approve_line("l1", "r2", "rev-1", T)
    svc.seal_line("l1", "seal-1", T)

    svc.begin_line("l2")
    svc.scan_bin("l2", "scan-2", "b-142", T)
    svc.record_event(ev("e4", line="l2", bin_code="b-142",
                        action=ScaleAction.TARE, reading="100"))
    svc.record_event(ev("e5", line="l2", bin_code="b-142",
                        action=ScaleAction.ADD, reading="106"))
    svc.record_event(ev("e6", line="l2", bin_code="b-142",
                        action=ScaleAction.ACCEPT, reading="106"))
    svc.approve_line("l2", "r2", "rev-2", T)
    svc.seal_line("l2", "seal-2", T)

    sheet = svc.generate_dispensing_sheet(T)
    locks = sheet["lines"][0]["line_locks"]
    assert len(locks) == 1
    assert locks[0]["reason"] == "identity_mismatch"
    assert sheet["lines"][0]["attempt_count"] == 2
    assert sheet["lines"][1]["line_locks"] == []


# -- 交班 -------------------------------------------------------------------

def test_handover_shows_lock_reasons_after_shift_change():
    svc = make_service()
    svc.begin_line("l1")
    with pytest.raises(IdentityMismatchError):
        svc.scan_bin("l1", "scan-1", "b-142", T)
    report = svc.handover_report(T)
    l1 = report["lines"][0]
    assert l1["status"] == "locked"
    assert l1["locks"][0]["reason"] == "identity_mismatch"
    assert l1["current_line"] is True
    # 接班人看到原因后重试仍可继续
    svc.retry_line("l1")
    assert svc.lines["l1"].attempt_no == 2
