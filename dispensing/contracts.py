"""处方药味、药斗身份、秤台事件与复核记录。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class ScaleAction(StrEnum):
    TARE = "tare"
    ADD = "add"
    REMOVE = "remove"
    ACCEPT = "accept"


class AttemptStatus(StrEnum):
    """单味药的一次称量尝试状态。"""

    ACTIVE = "active"        # 进行中
    LOCKED = "locked"        # 已被防错规则锁定
    COMPLETE = "complete"    # 称量合格
    INVALIDATED = "invalidated"  # 处方变更，未包装任务失效


class LockReason(StrEnum):
    """逐味锁定原因，须随记录长期保留（交班后仍可查）。"""

    IDENTITY_MISMATCH = "identity_mismatch"      # 药斗身份与处方药味不符
    BIN_DISABLED = "bin_disabled"                # 药斗已停用
    CALIBRATION_EXPIRED = "calibration_expired"  # 秤台校准失效
    TOLERANCE_EXCEEDED = "tolerance_exceeded"    # 超出剂量容差


@dataclass(frozen=True)
class PrescriptionLine:
    line_id: str
    snapshot_id: str
    herb_code: str
    display_name: str
    target_grams: Decimal
    tolerance_grams: Decimal


@dataclass(frozen=True)
class ScaleEvent:
    event_id: str
    device_id: str
    line_id: str
    bin_code: str
    action: ScaleAction
    reading_grams: Decimal
    occurred_at: datetime
    calibration_valid: bool


@dataclass(frozen=True)
class DispensingReview:
    review_id: str
    session_id: str
    dispenser_id: str
    reviewer_id: str
    package_seal: str | None
    reviewed_at: datetime
    unpacked: bool = False


@dataclass(frozen=True)
class Bin:
    """药斗：编码与内装药味身份绑定。"""

    code: str
    herb_code: str
    display_name: str
    enabled: bool = True


@dataclass(frozen=True)
class ScaleDevice:
    """电子秤：校准状态随设备登记，读数事件再各自快照一次。"""

    device_id: str
    calibration_valid: bool
    calibrated_until: date | None = None


@dataclass(frozen=True)
class LockRecord:
    """一次锁定的留痕，跨尝试、跨交班保留。"""

    line_id: str
    attempt_no: int
    reason: LockReason
    detail: str
    occurred_at: datetime
    device_id: str | None = None
    event_id: str | None = None
    scan_id: str | None = None
