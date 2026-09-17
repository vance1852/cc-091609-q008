"""调剂任务、药味状态、锁定记录与合格调剂单。

状态机：
    药味: PENDING -> ACTIVE -> ACCEPTED，任何时刻可因防错规则进入 LOCKED，
          组长解锁后回到 PENDING 重新称量（锁定记录永久保留）。
    任务: OPEN -> PACKAGED -> COMPLETED / RETURNED；
          处方变更时 OPEN/RETURNED -> VOIDED，PACKAGED -> UNSEAL_REVIEW。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .contracts import DispensingReview, PrescriptionLine, ScaleEvent


class LineStatus(StrEnum):
    PENDING = "pending"      # 待扫码
    ACTIVE = "active"        # 已扫码，称量中（秤台读数只归入该味）
    ACCEPTED = "accepted"    # 称量合格
    LOCKED = "locked"        # 已锁定，等待组长解锁


class LockReason(StrEnum):
    IDENTITY_MISMATCH = "identity-mismatch"      # 药斗与处方药味身份不符
    BIN_DISABLED = "bin-disabled"                # 药斗已停用
    CALIBRATION_EXPIRED = "calibration-expired"  # 秤台校准失效
    OUT_OF_TOLERANCE = "out-of-tolerance"        # 超出剂量容差


class TaskStatus(StrEnum):
    OPEN = "open"                    # 调剂中
    PACKAGED = "packaged"            # 已封袋
    COMPLETED = "completed"          # 复核通过
    RETURNED = "returned"            # 复核退回
    UNSEAL_REVIEW = "unseal-review"  # 已封袋遇处方变更，待拆包复核
    VOIDED = "voided"                # 已作废


@dataclass
class LockRecord:
    """一次锁定的完整留痕；解锁只补充字段，不删除记录，交班后仍可追溯。"""

    line_id: str
    reason: LockReason
    detail: str
    locked_at: datetime
    actor_id: str | None = None
    unlocked_by: str | None = None
    unlocked_at: datetime | None = None
    unlock_note: str | None = None

    @property
    def resolved(self) -> bool:
        return self.unlocked_by is not None


@dataclass
class AuditEntry:
    """换秤、解锁、封袋、作废等过程审计。"""

    at: datetime
    kind: str
    detail: str
    actor_id: str | None = None


@dataclass
class LoggedEvent:
    """按到达顺序保存的秤台事件；applied=False 表示被防错规则拦截。"""

    seq: int
    event: ScaleEvent
    applied: bool
    note: str


@dataclass
class LineState:
    """一味药的称量过程状态。去皮值绑定当前秤台，换秤即作废。"""

    line: PrescriptionLine
    expected_bin: str
    status: LineStatus = LineStatus.PENDING
    device_id: str | None = None
    tare_grams: Decimal | None = None
    net_grams: Decimal = Decimal("0")
    lock: LockRecord | None = None
    accepted_grams: Decimal | None = None
    accepted_device_id: str | None = None
    accepted_calibration_valid: bool | None = None
    accept_event_id: str | None = None


@dataclass
class DispensingTask:
    task_id: str
    snapshot_id: str
    dispenser_id: str
    lines: dict[str, LineState]
    status: TaskStatus = TaskStatus.OPEN
    active_line_id: str | None = None
    package_seal: str | None = None
    reviews: list[DispensingReview] = field(default_factory=list)
    lock_history: list[LockRecord] = field(default_factory=list)
    event_log: list[LoggedEvent] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    slip: DispensingSlip | None = None


@dataclass(frozen=True)
class ScanOutcome:
    """扫码结果（幂等：同一 scan_id 重发返回同一结果）。"""

    scan_id: str
    line_id: str
    matched: bool
    note: str
    lock: LockRecord | None = None


@dataclass(frozen=True)
class ScaleEventOutcome:
    """秤台事件处理结果（幂等：同一 event_id 重发返回同一结果）。"""

    event_id: str
    line_id: str
    applied: bool
    note: str
    lock: LockRecord | None = None


@dataclass(frozen=True)
class SlipLine:
    """合格调剂单中的一味：目标量、实际量、设备状态。"""

    line_id: str
    display_name: str
    target_grams: Decimal
    actual_grams: Decimal
    device_id: str
    calibration_valid: bool
    accept_event_id: str


@dataclass(frozen=True)
class DispensingSlip:
    """合格调剂单：逐味称量结果 + 调剂/复核双人责任。"""

    slip_id: str
    session_id: str
    snapshot_id: str
    package_seal: str
    lines: tuple[SlipLine, ...]
    dispenser_id: str
    reviewer_id: str
    issued_at: datetime


@dataclass(frozen=True)
class HandoverEntry:
    """交班报告中的一条锁定记录（含已解决的）。"""

    task_id: str
    task_status: TaskStatus
    line_id: str
    display_name: str
    reason: LockReason
    detail: str
    locked_at: datetime
    resolved: bool
    unlocked_by: str | None
