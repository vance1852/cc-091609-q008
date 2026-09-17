"""调剂防错领域服务。

核心原则：逐味负责。每味药单独扫码核验身份、单独去皮称量、单独双人复核，
任一袋总重相符都不能替代逐味校验。

规则要点：
- 调剂员领取药味后逐味扫描药斗，药斗身份必须与处方快照一致；
- 秤台读数只能归入当前药味，去皮/加减/回退全部保留事件顺序；
- 换秤后旧去皮值作废，必须在新秤重新去皮；
- 校准失效、药斗停用、超出剂量容差、身份不符立即锁定该味（当次尝试不可恢复，
  只能重新发起一次尝试，锁定留痕跨尝试长期保留）；
- 复核员不得复核自己的称量；处方变更使未封袋任务失效；已封袋只能拆包复核；
- 扫码与秤台事件按业务编号幂等，重放不产生重复效果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .contracts import (
    AttemptStatus,
    Bin,
    DispensingReview,
    LockReason,
    LockRecord,
    PrescriptionLine,
    ScaleAction,
    ScaleDevice,
    ScaleEvent,
)

ZERO = Decimal("0")


# --------------------------------------------------------------------------- 异常


class DispensingError(Exception):
    """所有调剂业务错误的基类。"""

    def __init__(self, message: str, *, lock: LockRecord | None = None):
        super().__init__(message)
        self.lock = lock


class IdentityMismatchError(DispensingError):
    """药斗身份与处方药味不符。"""


class BinDisabledError(DispensingError):
    """药斗已停用。"""


class CalibrationExpiredError(DispensingError):
    """秤台校准失效。"""


class ToleranceExceededError(DispensingError):
    """称量超出剂量容差。"""


class LineLockedError(DispensingError):
    """药味当前尝试已锁定，需重新发起尝试。"""


class NotCurrentLineError(DispensingError):
    """读数只能归入当前药味。"""


class StaleDeviceError(DispensingError):
    """读数来自已换下场的旧秤台。"""


class StaleTareError(DispensingError):
    """换秤后未重新去皮，旧去皮值不得沿用。"""


class InvalidReadingError(DispensingError):
    """读数本身不合法（如净重为负）。"""


class SelfReviewError(DispensingError):
    """复核员不得复核自己的称量。"""


class SealedPackageError(DispensingError):
    """已封袋只能进入拆包复核。"""


class InvalidatedTaskError(DispensingError):
    """处方已变更，未包装任务失效。"""


class SheetNotReadyError(DispensingError):
    """调剂单尚不能生成，并附带缺失/异常原因。"""


class IdempotencyConflictError(DispensingError):
    """同一业务编号重放但内容不一致。"""


# --------------------------------------------------------------------------- 内部状态


@dataclass
class _ScanRecord:
    scan_id: str
    line_id: str
    bin_code: str
    at: datetime
    ok: bool


@dataclass
class _Attempt:
    """单味药的一次称量尝试；锁定后不可恢复，只能另起一次。"""

    no: int
    status: AttemptStatus = AttemptStatus.ACTIVE
    scans: list[_ScanRecord] = field(default_factory=list)
    events: list[ScaleEvent] = field(default_factory=list)
    verified_bin: str | None = None
    active_tare_device: str | None = None
    tare_baseline: Decimal = ZERO
    net_grams: Decimal = ZERO
    lock: LockRecord | None = None
    accepted_device: str | None = None
    accepted_calibration_valid: bool | None = None


@dataclass
class _LineState:
    line: PrescriptionLine
    dispenser_id: str
    attempts: dict[int, _Attempt] = field(default_factory=dict)
    attempt_no: int = 0
    actual_grams: Decimal | None = None
    approved_by: str | None = None
    seal_id: str | None = None
    reviews: list[DispensingReview] = field(default_factory=list)
    invalidated: bool = False

    @property
    def attempt(self) -> _Attempt:
        return self.attempts[self.attempt_no]

    @property
    def sealed(self) -> bool:
        return self.seal_id is not None

    @property
    def complete(self) -> bool:
        return self.actual_grams is not None


# --------------------------------------------------------------------------- 服务


class DispensingService:
    def __init__(
        self,
        session_id: str,
        snapshot_id: str,
        dispenser_id: str,
        lines: list[PrescriptionLine],
        bins: list[Bin] | dict[str, Bin],
        scales: list[ScaleDevice] | dict[str, ScaleDevice],
        initial_device_id: str,
    ):
        self.session_id = session_id
        self.snapshot_id = snapshot_id
        self.dispenser_id = dispenser_id
        self.bins: dict[str, Bin] = {b.code: b for b in bins} if isinstance(bins, list) else dict(bins)
        self.scales: dict[str, ScaleDevice] = (
            {s.device_id: s for s in scales} if isinstance(scales, list) else dict(scales)
        )
        if initial_device_id not in self.scales:
            raise DispensingError(f"秤台未登记：{initial_device_id}")

        self.line_order = [ln.line_id for ln in lines]
        self.lines: dict[str, _LineState] = {
            ln.line_id: _LineState(line=ln, dispenser_id=dispenser_id) for ln in lines
        }
        self.current_device_id = initial_device_id
        self.current_line_id: str | None = None

        # 幂等表：同一编号重放只产生一次效果。
        self._seen_scans: dict[str, _ScanRecord] = {}
        self._seen_events: dict[str, ScaleEvent] = {}
        self._seen_reviews: dict[str, tuple[str, str]] = {}  # review_id -> (line_id, reviewer_id)

        # 锁定留痕：跨尝试保留，交班后仍可逐条看到原因。
        self.locks: list[LockRecord] = []

        # 处方变更标记：变更后未封袋任务一律失效。
        self.prescription_changed = False

    # -- 校准状态 -----------------------------------------------------------

    def set_calibration(self, device_id: str, valid: bool) -> None:
        """登记设备校准状态变化（如校准到期）。"""
        if device_id not in self.scales:
            raise DispensingError(f"秤台未登记：{device_id}")
        self.scales[device_id] = ScaleDevice(
            device_id=device_id,
            calibration_valid=valid,
            calibrated_until=self.scales[device_id].calibrated_until,
        )

    def _calibration_valid(self, device_id: str) -> bool:
        return self.scales[device_id].calibration_valid

    # -- 领取/重试 ----------------------------------------------------------

    def begin_line(self, line_id: str) -> _Attempt:
        """领取并开始一味药的第 1 次称量尝试。逐味串行。"""
        state = self._require_open_line(line_id)
        if self.current_line_id is not None:
            raise DispensingError(
                f"当前药味 {self.current_line_id} 尚未称量完成，不能转入 {line_id}"
            )
        self.current_line_id = line_id
        return self._new_attempt(state)

    def retry_line(self, line_id: str) -> _Attempt:
        """当前尝试被锁定后，重新发起一次尝试；历史尝试与锁定原因保留。"""
        state = self._require_open_line(line_id)
        if self.current_line_id != line_id:
            raise NotCurrentLineError(f"{line_id} 不是当前药味")
        current = state.attempt
        if current.status != AttemptStatus.LOCKED:
            raise DispensingError(f"{line_id} 当前尝试未锁定，无需重试")
        return self._new_attempt(state)

    def _new_attempt(self, state: _LineState) -> _Attempt:
        attempt = _Attempt(no=state.attempt_no + 1)
        state.attempt_no = attempt.no
        state.attempts[attempt.no] = attempt
        return attempt

    def _require_open_line(self, line_id: str) -> _LineState:
        state = self.lines.get(line_id)
        if state is None or state.line.snapshot_id != self.snapshot_id:
            raise DispensingError(f"药味不属于当前处方快照 {self.snapshot_id}：{line_id}")
        if state.invalidated:
            raise InvalidatedTaskError(f"{line_id} 已因处方变更失效")
        if state.sealed:
            raise SealedPackageError(f"{line_id} 已封袋")
        if state.complete:
            raise DispensingError(f"{line_id} 已称量合格")
        return state

    def _active_attempt(self, line_id: str) -> tuple[_LineState, _Attempt]:
        state = self.lines.get(line_id)
        if state is None or state.line.snapshot_id != self.snapshot_id:
            raise DispensingError(f"药味不属于当前处方快照 {self.snapshot_id}：{line_id}")
        if state.invalidated:
            raise InvalidatedTaskError(f"{line_id} 已因处方变更失效")
        if self.current_line_id != line_id:
            raise NotCurrentLineError(
                f"读数只能归入当前药味（当前：{self.current_line_id}，收到：{line_id}）"
            )
        attempt = state.attempt
        if attempt.status == AttemptStatus.LOCKED:
            assert attempt.lock is not None
            raise LineLockedError(
                f"{line_id} 第 {attempt.no} 次尝试已锁定：{attempt.lock.reason.value}",
                lock=attempt.lock,
            )
        if attempt.status != AttemptStatus.ACTIVE:
            raise DispensingError(f"{line_id} 当前尝试状态异常：{attempt.status.value}")
        return state, attempt

    def _lock(
        self,
        state: _LineState,
        attempt: _Attempt,
        reason: LockReason,
        detail: str,
        at: datetime,
        *,
        device_id: str | None = None,
        event_id: str | None = None,
        scan_id: str | None = None,
    ) -> None:
        record = LockRecord(
            line_id=state.line.line_id,
            attempt_no=attempt.no,
            reason=reason,
            detail=detail,
            occurred_at=at,
            device_id=device_id,
            event_id=event_id,
            scan_id=scan_id,
        )
        attempt.status = AttemptStatus.LOCKED
        attempt.lock = record
        self.locks.append(record)
        error_cls = {
            LockReason.IDENTITY_MISMATCH: IdentityMismatchError,
            LockReason.BIN_DISABLED: BinDisabledError,
            LockReason.CALIBRATION_EXPIRED: CalibrationExpiredError,
            LockReason.TOLERANCE_EXCEEDED: ToleranceExceededError,
        }[reason]
        raise error_cls(detail, lock=record)

    # -- 扫码核验 -----------------------------------------------------------

    def scan_bin(self, line_id: str, scan_id: str, bin_code: str, at: datetime) -> _ScanRecord:
        """扫描药斗条码，与处方快照中的药味身份逐味核对。幂等。"""
        if scan_id in self._seen_scans:
            seen = self._seen_scans[scan_id]
            if (seen.line_id, seen.bin_code) != (line_id, bin_code):
                raise IdempotencyConflictError(f"扫码编号 {scan_id} 重放内容不一致")
            return seen

        state, attempt = self._active_attempt(line_id)
        line = state.line
        record = _ScanRecord(scan_id=scan_id, line_id=line_id, bin_code=bin_code, at=at, ok=False)
        attempt.scans.append(record)
        self._seen_scans[scan_id] = record

        bin_ = self.bins.get(bin_code)
        if bin_ is None:
            self._lock(
                state, attempt, LockReason.IDENTITY_MISMATCH,
                f"药斗 {bin_code} 未登记，身份无法核验", at, scan_id=scan_id,
            )
        if not bin_.enabled:
            self._lock(
                state, attempt, LockReason.BIN_DISABLED,
                f"药斗 {bin_code}（{bin_.display_name}）已停用", at, scan_id=scan_id,
            )
        if bin_.herb_code != line.herb_code:
            self._lock(
                state, attempt, LockReason.IDENTITY_MISMATCH,
                f"药斗 {bin_code} 内为{bin_.display_name}，"
                f"与处方药味 {line.display_name} 不符",
                at, scan_id=scan_id,
            )

        record.ok = True
        attempt.verified_bin = bin_code
        return record

    # -- 换秤 ---------------------------------------------------------------

    def switch_scale(self, to_device_id: str, at: datetime) -> None:
        """换秤：旧去皮值作废，当前尝试必须在新秤重新去皮。幂等。"""
        if to_device_id not in self.scales:
            raise DispensingError(f"秤台未登记：{to_device_id}")
        if not self._calibration_valid(to_device_id):
            raise CalibrationExpiredError(f"秤台 {to_device_id} 校准失效，不能启用")
        if to_device_id == self.current_device_id:
            return

        self.current_device_id = to_device_id
        if self.current_line_id is not None:
            attempt = self.lines[self.current_line_id].attempt
            if attempt.status == AttemptStatus.ACTIVE:
                # 旧秤上的去皮与净重一律不得带到新秤；事件本身保留留痕。
                attempt.active_tare_device = None
                attempt.tare_baseline = ZERO
                attempt.net_grams = ZERO

    # -- 秤台事件 -----------------------------------------------------------

    def record_event(self, event: ScaleEvent) -> ScaleEvent:
        """归入一笔秤台读数。扫码与秤台重发按 event_id 幂等。"""
        seen = self._seen_events.get(event.event_id)
        if seen is not None:
            if _event_key(seen) != _event_key(event):
                raise IdempotencyConflictError(f"事件 {event.event_id} 重放内容不一致")
            return seen

        state, attempt = self._active_attempt(event.line_id)
        line = state.line

        # 读数只能来自当前绑定的秤台：旧秤迟到/重发一律拒收。
        if event.device_id != self.current_device_id:
            if event.device_id in self.scales:
                raise StaleDeviceError(
                    f"读数来自已换下场的秤台 {event.device_id}（当前：{self.current_device_id}）"
                )
            raise DispensingError(f"秤台未登记：{event.device_id}")

        # 读数只能来自已核验身份的药斗。
        if attempt.verified_bin is None:
            self._lock(
                state, attempt, LockReason.IDENTITY_MISMATCH,
                "未扫描药斗即上秤称量，身份未核验",
                event.occurred_at, device_id=event.device_id, event_id=event.event_id,
            )
        if event.bin_code != attempt.verified_bin:
            self._lock(
                state, attempt, LockReason.IDENTITY_MISMATCH,
                f"读数药斗 {event.bin_code} 与已核验药斗 {attempt.verified_bin} 不符",
                event.occurred_at, device_id=event.device_id, event_id=event.event_id,
            )

        # 校准状态以服务端登记为准，随事件快照保存。
        valid = self._calibration_valid(event.device_id) and event.calibration_valid
        stored = ScaleEvent(
            event_id=event.event_id,
            device_id=event.device_id,
            line_id=event.line_id,
            bin_code=event.bin_code,
            action=event.action,
            reading_grams=event.reading_grams,
            occurred_at=event.occurred_at,
            calibration_valid=valid,
        )
        attempt.events.append(stored)
        self._seen_events[event.event_id] = stored

        if not valid:
            self._lock(
                state, attempt, LockReason.CALIBRATION_EXPIRED,
                f"秤台 {event.device_id} 校准已失效，读数 {stored.reading_grams}g 不得采用",
                event.occurred_at, device_id=event.device_id, event_id=event.event_id,
            )

        if stored.action == ScaleAction.TARE:
            # 新去皮周期：旧净重清零，基线随新秤/新药斗重新建立。
            attempt.active_tare_device = stored.device_id
            attempt.tare_baseline = stored.reading_grams
            attempt.net_grams = ZERO
            return stored

        if stored.action in (ScaleAction.ADD, ScaleAction.REMOVE):
            if attempt.active_tare_device != stored.device_id:
                raise StaleTareError(
                    f"秤台 {stored.device_id} 尚未去皮，不得沿用旧秤去皮值"
                )
            net = stored.reading_grams - attempt.tare_baseline
            if net < ZERO:
                raise InvalidReadingError(
                    f"净重 {net}g 为负，读数 {stored.reading_grams}g 低于去皮基线"
                )
            # 加减与回退（取药）只是过程量，逐笔保留；加多了允许回退取药，
            # 是否超出剂量容差在确认（ACCEPT）时一次性裁定并立即锁定。
            attempt.net_grams = net
            return stored

        if stored.action == ScaleAction.ACCEPT:
            net = attempt.net_grams
            low = line.target_grams - line.tolerance_grams
            high = line.target_grams + line.tolerance_grams
            if not (low <= net <= high):
                self._lock(
                    state, attempt, LockReason.TOLERANCE_EXCEEDED,
                    f"确认净重 {net}g 超出容差区间 [{low}g, {high}g]"
                    f"（目标 {line.target_grams}g）",
                    event.occurred_at, device_id=stored.device_id,
                    event_id=stored.event_id,
                )
            attempt.status = AttemptStatus.COMPLETE
            attempt.accepted_device = stored.device_id
            attempt.accepted_calibration_valid = valid
            state.actual_grams = net
            self.current_line_id = None
            return stored

        raise DispensingError(f"未知秤台动作：{stored.action}")

    # -- 双人复核与封袋 ------------------------------------------------------

    def approve_line(
        self,
        line_id: str,
        reviewer_id: str,
        review_id: str,
        at: datetime,
        *,
        unpacked: bool = False,
    ) -> DispensingReview:
        """复核员逐味复核。不得复核本人称量；已封袋只允许拆包复核。幂等。"""
        if review_id in self._seen_reviews:
            if self._seen_reviews[review_id] != (line_id, reviewer_id):
                raise IdempotencyConflictError(f"复核编号 {review_id} 重放内容不一致")
            return next(r for r in self.lines[line_id].reviews if r.review_id == review_id)

        state = self.lines[line_id]
        if reviewer_id == state.dispenser_id:
            raise SelfReviewError(
                f"复核员 {reviewer_id} 不得复核自己的称量（{line_id}）"
            )

        if state.sealed:
            if not unpacked:
                raise SealedPackageError(
                    f"{line_id} 已封袋（{state.seal_id}），只能进入拆包复核"
                )
            review = DispensingReview(
                review_id=review_id,
                session_id=self.session_id,
                dispenser_id=state.dispenser_id,
                reviewer_id=reviewer_id,
                package_seal=state.seal_id,
                reviewed_at=at,
                unpacked=True,
            )
        else:
            if not state.complete:
                raise DispensingError(f"{line_id} 尚未称量合格，不能复核")
            if state.invalidated:
                raise InvalidatedTaskError(f"{line_id} 已因处方变更失效")
            if unpacked:
                raise SealedPackageError(f"{line_id} 尚未封袋，不能拆包复核")
            review = DispensingReview(
                review_id=review_id,
                session_id=self.session_id,
                dispenser_id=state.dispenser_id,
                reviewer_id=reviewer_id,
                package_seal=None,
                reviewed_at=at,
                unpacked=False,
            )
            state.approved_by = reviewer_id

        state.reviews.append(review)
        self._seen_reviews[review_id] = (line_id, reviewer_id)
        return review

    def seal_line(self, line_id: str, seal_id: str, at: datetime) -> None:
        """复核通过后封袋。"""
        state = self.lines[line_id]
        if state.invalidated:
            raise InvalidatedTaskError(f"{line_id} 已因处方变更失效")
        if not state.complete:
            raise DispensingError(f"{line_id} 尚未称量合格，不能封袋")
        if state.approved_by is None:
            raise DispensingError(f"{line_id} 未经第二人复核，不能封袋")
        if state.sealed:
            if state.seal_id != seal_id:
                raise SealedPackageError(f"{line_id} 已封袋：{state.seal_id}")
            return
        state.seal_id = seal_id

    # -- 处方变更 -----------------------------------------------------------

    def apply_prescription_change(self, at: datetime) -> None:
        """处方变更：所有未封袋药味的未包装任务立即失效；已封袋不受影响。"""
        self.prescription_changed = True
        for state in self.lines.values():
            if state.sealed:
                continue
            state.invalidated = True
            state.approved_by = None
            state.actual_grams = None
            for attempt in state.attempts.values():
                if attempt.status in (AttemptStatus.ACTIVE, AttemptStatus.COMPLETE):
                    attempt.status = AttemptStatus.INVALIDATED
        self.current_line_id = None

    # -- 调剂单 -------------------------------------------------------------

    def is_qualified(self) -> bool:
        return all(
            s.sealed and s.complete and not s.invalidated and s.approved_by is not None
            for s in self.lines.values()
        )

    def handover_report(self, at: datetime) -> dict[str, Any]:
        """交班记录：逐味当前状态与全部历史锁定原因，接班人可直接续办。"""
        return {
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "current_device_id": self.current_device_id,
            "prescription_changed": self.prescription_changed,
            "generated_at": at.isoformat(),
            "lines": [
                {
                    "line_id": lid,
                    "status": self._line_status(lid),
                    "attempts": self.lines[lid].attempt_no,
                    "current_line": self.current_line_id == lid,
                    "actual_grams": (
                        str(self.lines[lid].actual_grams)
                        if self.lines[lid].actual_grams is not None
                        else None
                    ),
                    "reviewer_id": self.lines[lid].approved_by,
                    "seal_id": self.lines[lid].seal_id,
                    "locks": [
                        _lock_dict(a.lock)
                        for a in self.lines[lid].attempts.values()
                        if a.lock is not None
                    ],
                }
                for lid in self.line_order
            ],
        }

    def _line_status(self, line_id: str) -> str:
        state = self.lines[line_id]
        if state.invalidated:
            return "invalidated"
        if state.sealed:
            return "sealed"
        if state.complete:
            return "weighed"
        if state.attempt_no == 0:
            return "not_started"
        current = state.attempt
        if current.status == AttemptStatus.LOCKED:
            return "locked"
        return "in_progress"

    def generate_dispensing_sheet(self, generated_at: datetime) -> dict[str, Any]:
        """全部药味合格封袋后生成唯一一份合格调剂单（逐味明细）。"""
        blockers: list[str] = []
        for line_id in self.line_order:
            state = self.lines[line_id]
            if state.invalidated:
                blockers.append(f"{line_id} 已因处方变更失效，需按新处方重新调剂")
            elif not state.complete:
                locks = [a.lock for a in state.attempts.values() if a.lock is not None]
                if locks:
                    latest = locks[-1]
                    blockers.append(
                        f"{line_id} 未完成，最近锁定：{latest.reason.value}（{latest.detail}）"
                    )
                else:
                    blockers.append(f"{line_id} 尚未称量")
            elif state.approved_by is None:
                blockers.append(f"{line_id} 未经第二人复核")
            elif not state.sealed:
                blockers.append(f"{line_id} 未封袋")
        if blockers:
            raise SheetNotReadyError("调剂单未就绪：" + "；".join(blockers))

        return {
            "document": "中药调剂合格单",
            "session_id": self.session_id,
            "snapshot_id": self.snapshot_id,
            "dispenser_id": self.dispenser_id,
            "generated_at": generated_at.isoformat(),
            "qualified": True,
            "lines": [self._line_sheet(line_id) for line_id in self.line_order],
            "lock_audit": [_lock_dict(r) for r in self.locks],
        }

    def _line_sheet(self, line_id: str) -> dict[str, Any]:
        state = self.lines[line_id]
        line = state.line
        attempt = next(
            a for a in reversed(list(state.attempts.values()))
            if a.status == AttemptStatus.COMPLETE
        )
        bin_ = self.bins[attempt.verified_bin] if attempt.verified_bin else None
        return {
            "line_id": line_id,
            "herb_code": line.herb_code,
            "display_name": line.display_name,
            "bin_code": attempt.verified_bin,
            "bin_identity": bin_.display_name if bin_ else None,
            "target_grams": str(line.target_grams),
            "tolerance_grams": str(line.tolerance_grams),
            "actual_grams": str(state.actual_grams),
            "device": {
                "device_id": attempt.accepted_device,
                "calibration_valid": attempt.accepted_calibration_valid,
            },
            "dispenser_id": state.dispenser_id,
            "reviewer_id": state.approved_by,
            "package_seal": state.seal_id,
            "attempt_count": state.attempt_no,
            "line_locks": [
                _lock_dict(a.lock) for a in state.attempts.values() if a.lock is not None
            ],
        }


# --------------------------------------------------------------------------- 辅助


def _event_key(e: ScaleEvent) -> tuple:
    return (
        e.device_id, e.line_id, e.bin_code, str(e.action),
        str(e.reading_grams), e.occurred_at.isoformat(), e.calibration_valid,
    )


def _lock_dict(r: LockRecord) -> dict[str, Any]:
    return {
        "line_id": r.line_id,
        "attempt_no": r.attempt_no,
        "reason": r.reason.value,
        "detail": r.detail,
        "occurred_at": r.occurred_at.isoformat(),
        "device_id": r.device_id,
        "event_id": r.event_id,
        "scan_id": r.scan_id,
    }
