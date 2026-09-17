"""按 fixtures/weighing_session.json 还原错斗、校准失效、换秤与回退全过程。

回放脚本可直接运行：

    python -m dispensing.replay

只在全部药味逐味合格并封袋后输出唯一一份《中药调剂合格单》。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import (
    Bin,
    LockReason,
    PrescriptionLine,
    ScaleAction,
    ScaleDevice,
    ScaleEvent,
)
from .service import (
    CalibrationExpiredError,
    DispensingService,
    IdentityMismatchError,
    SealedPackageError,
    SelfReviewError,
    StaleDeviceError,
    StaleTareError,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "weighing_session.json"

DEFAULT_TOLERANCE_GRAMS = Decimal("0.5")


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def build_service(fixture: dict[str, Any]) -> DispensingService:
    """依据快照建立药斗身份档案与秤台台账。

    初始秤台 scale-2 校准刚过期（对应 fixture 的 calibration-expired 标记），
    备用秤 scale-5 校准有效。
    """
    # 药斗编码 -> 药味身份编码；名称相近（制南星/胆南星）但身份编码不同。
    herb_identities = {
        "制南星": "herb-zhi-nanxing",
        "胆南星": "herb-dan-nanxing",
    }
    bins: list[Bin] = []
    for ln in fixture["lines"]:
        bins.append(Bin(
            code=ln["bin"],
            herb_code=herb_identities[ln["name"]],
            display_name=ln["name"],
        ))
    scales = [
        ScaleDevice(device_id="scale-2", calibration_valid=True),
        ScaleDevice(device_id="scale-5", calibration_valid=True),
    ]
    lines = [
        PrescriptionLine(
            line_id=ln["id"],
            snapshot_id=fixture["snapshot"],
            herb_code=herb_identities[ln["name"]],
            display_name=ln["name"],
            target_grams=Decimal(ln["targetGrams"]),
            tolerance_grams=DEFAULT_TOLERANCE_GRAMS,
        )
        for ln in fixture["lines"]
    ]
    return DispensingService(
        session_id=fixture["session"],
        snapshot_id=fixture["snapshot"],
        dispenser_id="d-lin",
        lines=lines,
        bins=bins,
        scales=scales,
        initial_device_id="scale-2",
    )


def _ev(
    seq: int,
    base: datetime,
    *,
    device: str,
    line: str,
    bin_code: str,
    action: ScaleAction,
    reading: str,
    calibrated: bool,
) -> ScaleEvent:
    return ScaleEvent(
        event_id=f"evt-{seq:03d}",
        device_id=device,
        line_id=line,
        bin_code=bin_code,
        action=action,
        reading_grams=Decimal(reading),
        occurred_at=base + timedelta(seconds=seq * 5),
        calibration_valid=calibrated,
    )


def replay(fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    fixture = fixture or load_fixture()
    svc = build_service(fixture)
    t0 = datetime(2026, 9, 17, 9, 0, 0)
    seq = 0

    def next_ev(**kw):  # noqa: ANN001
        nonlocal seq
        seq += 1
        return _ev(seq, t0, **kw)

    markers = {m["kind"]: m for m in fixture["events"]}

    # -- line-a 制南星：第 1 次尝试，扫错药斗（拿成名近的胆南星 bin-142） -----
    svc.begin_line("line-a")
    wrong_bin = markers["wrong-bin"]["scanned"]
    try:
        svc.scan_bin("line-a", scan_id="scan-a-1", bin_code=wrong_bin, at=t0)
    except IdentityMismatchError as exc:
        assert exc.lock and exc.lock.reason == LockReason.IDENTITY_MISMATCH

    # 第 2 次尝试：改扫正确药斗 bin-141；此时 scale-2 校准刚过期。
    svc.retry_line("line-a")
    svc.scan_bin("line-a", scan_id="scan-a-2", bin_code="bin-141", at=t0)
    svc.set_calibration(markers["calibration-expired"]["scale"], valid=False)
    try:
        svc.record_event(next_ev(
            device="scale-2", line="line-a", bin_code="bin-141",
            action=ScaleAction.TARE, reading="0.0", calibrated=False,
        ))
    except CalibrationExpiredError as exc:
        assert exc.lock and exc.lock.reason == LockReason.CALIBRATION_EXPIRED

    # 换秤到 scale-5；旧秤去皮值作废。第 3 次尝试在新秤重新扫码去皮。
    svc.switch_scale(markers["switch-scale"]["to"], at=t0)
    svc.retry_line("line-a")
    svc.scan_bin("line-a", scan_id="scan-a-3", bin_code="bin-141", at=t0)

    # 旧秤迟到/重发的读数不得归入当前药味。
    try:
        svc.record_event(next_ev(
            device="scale-2", line="line-a", bin_code="bin-141",
            action=ScaleAction.ADD, reading="0.0", calibrated=False,
        ))
    except StaleDeviceError:
        pass
    # 新秤未去皮，不得沿用旧去皮值。
    try:
        svc.record_event(next_ev(
            device="scale-5", line="line-a", bin_code="bin-141",
            action=ScaleAction.ADD, reading="9.0", calibrated=True,
        ))
    except StaleTareError:
        pass

    # 正式称量：去皮 → 加多 → 回退取药 → 确认；全部事件保留。
    tare = next_ev(device="scale-5", line="line-a", bin_code="bin-141",
                   action=ScaleAction.TARE, reading="120.0", calibrated=True)
    svc.record_event(tare)
    svc.record_event(next_ev(device="scale-5", line="line-a", bin_code="bin-141",
                             action=ScaleAction.ADD, reading="129.6", calibrated=True))
    svc.record_event(next_ev(device="scale-5", line="line-a", bin_code="bin-141",
                             action=ScaleAction.REMOVE, reading="129.1", calibrated=True))

    # 秤台重发幂等：同一 event_id 再收一遍，事件不重复、净重不变。
    svc.record_event(tare)
    svc.record_event(tare)
    # 扫码重发幂等：同一 scan_id 重放不产生重复扫码。
    svc.scan_bin("line-a", scan_id="scan-a-3", bin_code="bin-141", at=t0)

    svc.record_event(next_ev(device="scale-5", line="line-a", bin_code="bin-141",
                             action=ScaleAction.ACCEPT, reading="129.1", calibrated=True))

    # 复核员不得复核自己的称量。
    try:
        svc.approve_line("line-a", reviewer_id="d-lin", review_id="rev-a-self", at=t0)
    except SelfReviewError:
        pass
    # 第二人复核通过后封袋。
    svc.approve_line("line-a", reviewer_id="r-chen", review_id="rev-a-1", at=t0)
    svc.seal_line("line-a", seal_id="seal-a", at=t0)
    # 已封袋只能进入拆包复核（普通复核入口拒收）。
    try:
        svc.approve_line("line-a", reviewer_id="r-chen", review_id="rev-a-again", at=t0)
    except SealedPackageError:
        pass
    svc.approve_line("line-a", reviewer_id="r-chen", review_id="rev-a-unpack",
                     at=t0, unpacked=True)
    # 复核记录幂等：拆包复核重放只返回同一条。
    again = svc.approve_line("line-a", reviewer_id="r-chen", review_id="rev-a-unpack",
                             at=t0, unpacked=True)
    assert sum(r.review_id == "rev-a-unpack" for r in svc.lines["line-a"].reviews) == 1
    assert again.unpacked is True

    # -- line-b 胆南星：名称相近，逐味独立扫码称量，不靠总重兜底 -------------
    svc.begin_line("line-b")
    svc.scan_bin("line-b", scan_id="scan-b-1", bin_code="bin-142", at=t0)
    svc.record_event(next_ev(device="scale-5", line="line-b", bin_code="bin-142",
                             action=ScaleAction.TARE, reading="110.0", calibrated=True))
    svc.record_event(next_ev(device="scale-5", line="line-b", bin_code="bin-142",
                             action=ScaleAction.ADD, reading="116.0", calibrated=True))
    svc.record_event(next_ev(device="scale-5", line="line-b", bin_code="bin-142",
                             action=ScaleAction.ACCEPT, reading="116.0", calibrated=True))
    svc.approve_line("line-b", reviewer_id="r-chen", review_id="rev-b-1", at=t0)
    svc.seal_line("line-b", seal_id="seal-b", at=t0)

    return svc.generate_dispensing_sheet(datetime(2026, 9, 17, 9, 30, 0))


def main() -> None:
    sheet = replay()
    print(json.dumps(sheet, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
