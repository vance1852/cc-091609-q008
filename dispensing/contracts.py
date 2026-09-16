"""处方药味、秤台事件与复核记录。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class ScaleAction(StrEnum):
    TARE = "tare"
    ADD = "add"
    REMOVE = "remove"
    ACCEPT = "accept"


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
