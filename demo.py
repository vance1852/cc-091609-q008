"""按 fixtures/weighing_session.json 还原一次调剂全过程。

场景：错斗（制南星/胆南星）→ 秤台校准失效 → 换秤 → 回退 → 双人复核，
另演示处方变更（未包装作废、已封袋拆包复核）与交班锁定追溯。

运行：python3 demo.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dispensing import (
    DispensingService,
    InvalidStateError,
    PrescriptionLine,
    ScaleAction,
    ScaleEvent,
    SelfReviewError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "weighing_session.json"
T0 = datetime(2026, 9, 17, 8, 0, 0)
TOLERANCE = Decimal("0.5")

DISPENSER = "disp-01"   # 调剂员
SUPERVISOR = "lead-01"  # 调剂组长
REVIEWER = "rev-02"     # 复核员


def at(minute: int) -> datetime:
    return T0 + timedelta(minutes=minute)


def say(text: str) -> None:
    print(f"  {text}")


def make_event(
    event_id: str, device: str, line: str, bin_code: str,
    action: ScaleAction, reading: str, minute: int, calibration: bool = True,
) -> ScaleEvent:
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


def main() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session_id = fixture["session"]          # dispense-501
    snapshot_id = fixture["snapshot"]        # rx-88-v2
    anomaly = {event["kind"]: event for event in fixture["events"]}
    wrong_bin = anomaly["wrong-bin"]                 # 错斗：line-a 扫到 bin-142
    expired_scale = anomaly["calibration-expired"]["scale"]   # scale-2
    switch = anomaly["switch-scale"]                 # scale-2 -> scale-5
    svc = DispensingService()

    print("== 1. 领取任务（处方快照 %s，会话 %s）==" % (snapshot_id, session_id))
    lines = [
        PrescriptionLine(
            line_id=line["id"],
            snapshot_id=snapshot_id,
            herb_code=line["name"],
            display_name=line["name"],
            target_grams=Decimal(line["targetGrams"]),
            tolerance_grams=TOLERANCE,
        )
        for line in fixture["lines"]
    ]
    bins = {line["id"]: line["bin"] for line in fixture["lines"]}
    svc.claim_task(session_id, DISPENSER, lines, bins, at=at(0))
    for line in fixture["lines"]:
        say(f"药味 {line['id']}: {line['name']} 目标 {line['targetGrams']}g±{TOLERANCE}g 药斗 {line['bin']}")

    print("\n== 2. 错斗：%s 扫到 %s（fixture: wrong-bin）==" % (wrong_bin["line"], wrong_bin["scanned"]))
    outcome = svc.scan_bin(session_id, wrong_bin["line"], wrong_bin["scanned"], scan_id="scan-001", at=at(1), operator_id=DISPENSER)
    say(f"扫码结果 matched={outcome.matched} note={outcome.note}")
    say(f"锁定原因: {outcome.lock.reason} — {outcome.lock.detail}")
    again = svc.scan_bin(session_id, wrong_bin["line"], wrong_bin["scanned"], scan_id="scan-001", at=at(2), operator_id=DISPENSER)
    assert again is outcome, "扫码重发必须幂等"
    say("同一 scan_id 重发 -> 返回首次结果，未重复锁定（幂等）")
    svc.unlock_line(session_id, "line-a", supervisor_id=SUPERVISOR, note="组长确认错斗，重新扫码", at=at(3))

    print("\n== 3. 校准失效：%s 上报过期读数（fixture: calibration-expired）==" % expired_scale)
    svc.scan_bin(session_id, "line-a", "bin-141", scan_id="scan-002", at=at(4), operator_id=DISPENSER)
    svc.record_scale_event(session_id, make_event("ev-001", expired_scale, "line-a", "bin-141", ScaleAction.TARE, "2.0", 5))
    outcome = svc.record_scale_event(
        session_id, make_event("ev-002", expired_scale, "line-a", "bin-141", ScaleAction.ADD, "9.6", 6, calibration=False)
    )
    say(f"校准失效读数 applied={outcome.applied} note={outcome.note}")
    say(f"锁定原因: {outcome.lock.reason} — {outcome.lock.detail}")
    line_a = svc.task(session_id).lines["line-a"]
    assert line_a.net_grams == Decimal("0"), "失效读数不得计入净重"
    say("失效读数已留痕但未计入净重")
    svc.unlock_line(session_id, "line-a", supervisor_id=SUPERVISOR, note=f"{expired_scale} 停用送检，换秤重称", at=at(7))

    print("\n== 4. 换秤 %s -> %s（fixture: switch-scale），旧去皮值作废 ==" % (switch["from"], switch["to"]))
    svc.scan_bin(session_id, "line-a", "bin-141", scan_id="scan-003", at=at(8), operator_id=DISPENSER)
    svc.switch_scale(session_id, "line-a", switch["to"], at=at(9), operator_id=DISPENSER)
    outcome = svc.record_scale_event(session_id, make_event("ev-003", switch["to"], "line-a", "bin-141", ScaleAction.ADD, "9.6", 10))
    say(f"换秤后未去皮直接加料: applied={outcome.applied} note={outcome.note}（旧去皮值不得沿用）")
    svc.record_scale_event(session_id, make_event("ev-004", switch["to"], "line-a", "bin-141", ScaleAction.TARE, "2.0", 11))
    svc.record_scale_event(session_id, make_event("ev-005", switch["to"], "line-a", "bin-141", ScaleAction.ADD, "9.6", 12))
    outcome = svc.record_scale_event(session_id, make_event("ev-006", switch["to"], "line-a", "bin-141", ScaleAction.REMOVE, "0.6", 13))
    say(f"回退 0.6g: applied={outcome.applied} 当前净重 {svc.task(session_id).lines['line-a'].net_grams}g")
    outcome = svc.record_scale_event(session_id, make_event("ev-007", switch["to"], "line-a", "bin-141", ScaleAction.ACCEPT, "9.0", 14))
    say(f"确认称量: note={outcome.note}")
    again = svc.record_scale_event(session_id, make_event("ev-007", switch["to"], "line-a", "bin-141", ScaleAction.ACCEPT, "9.0", 14))
    assert again is outcome, "秤台事件重发必须幂等"
    say("同一 event_id 重发 -> 返回首次结果，未重复入账（幂等）")

    print("\n== 5. 第二味：胆南星（含回退）==")
    svc.scan_bin(session_id, "line-b", "bin-142", scan_id="scan-004", at=at(15), operator_id=DISPENSER)
    svc.record_scale_event(session_id, make_event("ev-008", switch["to"], "line-b", "bin-142", ScaleAction.TARE, "1.5", 16))
    svc.record_scale_event(session_id, make_event("ev-009", switch["to"], "line-b", "bin-142", ScaleAction.ADD, "6.3", 17))
    svc.record_scale_event(session_id, make_event("ev-010", switch["to"], "line-b", "bin-142", ScaleAction.REMOVE, "0.3", 18))
    outcome = svc.record_scale_event(session_id, make_event("ev-011", switch["to"], "line-b", "bin-142", ScaleAction.ACCEPT, "6.0", 19))
    say(f"确认称量: note={outcome.note}")

    print("\n== 6. 封袋与双人复核 ==")
    svc.seal_package(session_id, seal="seal-9001", at=at(20), operator_id=DISPENSER)
    try:
        svc.submit_review(session_id, "rv-000", DISPENSER, approve=True, at=at(21))
    except SelfReviewError as exc:
        say(f"调剂员自复核被拒: {exc}")
    svc.submit_review(session_id, "rv-001", REVIEWER, approve=True, at=at(22))
    say(f"复核员 {REVIEWER} 复核通过")

    print("\n== 7. 合格调剂单（全系统仅一份）==")
    slips = svc.qualified_slips()
    assert len(slips) == 1, "样例完成时应只生成一份合格调剂单"
    slip = slips[0]
    assert svc.qualified_slip(session_id) is slip, "重复开具必须返回同一份"
    print(f"  调剂单 {slip.slip_id} | 会话 {slip.session_id} | 快照 {slip.snapshot_id} | 封袋 {slip.package_seal}")
    print(f"  调剂员 {slip.dispenser_id} | 复核员 {slip.reviewer_id} | 开具于 {slip.issued_at:%H:%M}")
    print(f"  {'药味':<6}{'目标量':>8}{'实际量':>8}{'秤台':>10}{'校准':>6}")
    for item in slip.lines:
        print(f"  {item.display_name:<6}{item.target_grams:>7}g{item.actual_grams:>7}g{item.device_id:>10}{'有效' if item.calibration_valid else '失效':>6}")

    print("\n== 8. 处方变更：rx-88-v2 -> rx-88-v3 ==")
    other = [
        PrescriptionLine("line-a", snapshot_id, "制南星", "制南星", Decimal("9.0"), TOLERANCE),
        PrescriptionLine("line-b", snapshot_id, "胆南星", "胆南星", Decimal("6.0"), TOLERANCE),
    ]
    svc.claim_task("dispense-502", "disp-03", other, bins, at=at(23))  # 未包装
    svc.claim_task("dispense-503", "disp-04", other, bins, at=at(23))  # 将封袋
    svc.scan_bin("dispense-503", "line-a", "bin-141", scan_id="scan-101", at=at(24), operator_id="disp-04")
    svc.record_scale_event("dispense-503", make_event("ev-101", "scale-5", "line-a", "bin-141", ScaleAction.TARE, "2.0", 25))
    svc.record_scale_event("dispense-503", make_event("ev-102", "scale-5", "line-a", "bin-141", ScaleAction.ADD, "9.0", 26))
    svc.record_scale_event("dispense-503", make_event("ev-103", "scale-5", "line-a", "bin-141", ScaleAction.ACCEPT, "9.0", 27))
    svc.scan_bin("dispense-503", "line-b", "bin-142", scan_id="scan-102", at=at(28), operator_id="disp-04")
    svc.record_scale_event("dispense-503", make_event("ev-104", "scale-5", "line-b", "bin-142", ScaleAction.TARE, "1.5", 29))
    svc.record_scale_event("dispense-503", make_event("ev-105", "scale-5", "line-b", "bin-142", ScaleAction.ADD, "6.0", 30))
    svc.record_scale_event("dispense-503", make_event("ev-106", "scale-5", "line-b", "bin-142", ScaleAction.ACCEPT, "6.0", 31))
    svc.seal_package("dispense-503", seal="seal-9002", at=at(32), operator_id="disp-04")

    affected = svc.apply_prescription_change(snapshot_id, "rx-88-v3", at=at(33), operator_id="pharmacist-01")
    say(f"受影响任务: {affected}")
    say(f"dispense-502（未包装）-> {svc.task('dispense-502').status}")
    say(f"dispense-503（已封袋）-> {svc.task('dispense-503').status}（只能拆包复核）")
    try:
        svc.record_scale_event("dispense-503", make_event("ev-107", "scale-5", "line-a", "bin-141", ScaleAction.ADD, "0.1", 34))
    except InvalidStateError as exc:
        say(f"已封袋任务拒绝继续称量: {exc}")
    svc.complete_unseal_review("dispense-503", REVIEWER, decision="void", at=at(35), note="拆包核对与新版处方不符，作废重配")
    say(f"拆包复核后 dispense-503 -> {svc.task('dispense-503').status}")
    assert len(svc.qualified_slips()) == 1, "处方变更后仍只有一份合格调剂单"

    print("\n== 9. 交班报告：锁定原因永久可查 ==")
    for entry in svc.handover_report():
        state = "已解决" if entry.resolved else "未解决"
        by = f"，解锁人 {entry.unlocked_by}" if entry.unlocked_by else ""
        print(f"  [{entry.locked_at:%H:%M}] {entry.task_id}/{entry.line_id} {entry.display_name} "
              f"{entry.reason.value}: {entry.detail}（{state}{by}）")

    print("\n全部断言通过：样例完成，系统内仅一份合格调剂单。")


if __name__ == "__main__":
    main()
