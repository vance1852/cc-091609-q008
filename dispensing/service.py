"""调剂防错服务：逐味身份核验、秤台事件归口、锁定与双人复核。

防错规则：
  * 调剂员领取任务后逐味扫码，药斗与处方快照不符或药斗停用立即锁定该味；
  * 秤台读数只归入当前药味，去皮/加减/回退全部按顺序留痕；
  * 校准失效、超出剂量容差立即锁定该味；
  * 换秤后旧去皮值作废，新秤必须重新去皮；
  * 复核员不得复核自己的称量；
  * 处方变更使未包装任务作废，已封袋任务只能进入拆包复核；
  * 扫码与秤台事件按标识幂等，重发不产生副作用；
  * 锁定记录永久保留，交班后仍可查看原因。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping

from .contracts import DispensingReview, PrescriptionLine, ScaleAction, ScaleEvent
from .errors import (
    DuplicateTaskError,
    InvalidStateError,
    SelfReviewError,
    UnknownLineError,
    UnknownTaskError,
)
from .models import (
    AuditEntry,
    DispensingSlip,
    DispensingTask,
    HandoverEntry,
    LineState,
    LineStatus,
    LockReason,
    LockRecord,
    LoggedEvent,
    ScaleEventOutcome,
    ScanOutcome,
    SlipLine,
    TaskStatus,
)


class DispensingService:
    def __init__(self) -> None:
        self._tasks: dict[str, DispensingTask] = {}
        self._disabled_bins: set[str] = set()
        self._scans: dict[tuple[str, str], ScanOutcome] = {}
        self._events: dict[str, ScaleEventOutcome] = {}
        self._reviews: dict[str, DispensingReview] = {}

    # ------------------------------------------------------------------ 任务

    def claim_task(
        self,
        task_id: str,
        dispenser_id: str,
        lines: Iterable[PrescriptionLine],
        expected_bins: Mapping[str, str],
        *,
        at: datetime,
    ) -> DispensingTask:
        """调剂员领取任务：登记处方快照与每味药的药斗分配。"""
        if task_id in self._tasks:
            raise DuplicateTaskError(f"任务 {task_id} 已存在")
        lines = list(lines)
        if not lines:
            raise ValueError("处方至少包含一味药")
        if len({line.line_id for line in lines}) != len(lines):
            raise ValueError("药味编号重复")
        snapshots = {line.snapshot_id for line in lines}
        if len(snapshots) != 1:
            raise ValueError("同一任务的药味必须来自同一处方快照")
        missing = [line.line_id for line in lines if line.line_id not in expected_bins]
        if missing:
            raise ValueError(f"缺少药斗分配: {missing}")
        states = {
            line.line_id: LineState(line=line, expected_bin=expected_bins[line.line_id])
            for line in lines
        }
        task = DispensingTask(
            task_id=task_id,
            snapshot_id=lines[0].snapshot_id,
            dispenser_id=dispenser_id,
            lines=states,
        )
        task.audit.append(
            AuditEntry(at=at, kind="claim", detail=f"调剂员 {dispenser_id} 领取任务", actor_id=dispenser_id)
        )
        self._tasks[task_id] = task
        return task

    def task(self, task_id: str) -> DispensingTask:
        return self._get_task(task_id)

    # ------------------------------------------------------------------ 药斗

    def disable_bin(self, bin_code: str) -> None:
        self._disabled_bins.add(bin_code)

    def enable_bin(self, bin_code: str) -> None:
        self._disabled_bins.discard(bin_code)

    # ------------------------------------------------------------------ 扫码

    def scan_bin(
        self,
        task_id: str,
        line_id: str,
        scanned_bin: str,
        *,
        scan_id: str,
        at: datetime,
        operator_id: str | None = None,
    ) -> ScanOutcome:
        """逐味扫描药斗，与处方快照核对身份；错斗或停用斗立即锁定。"""
        key = (task_id, scan_id)
        if key in self._scans:  # 幂等：重发返回首次结果
            return self._scans[key]
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.OPEN, action="扫码")
        line = self._get_line(task, line_id)
        if line.status is not LineStatus.PENDING:
            raise InvalidStateError(f"药味 {line_id} 状态为 {line.status}，不能扫码")
        if task.active_line_id is not None:
            raise InvalidStateError(f"药味 {task.active_line_id} 正在称量，不能切换药味")

        if scanned_bin in self._disabled_bins:
            lock = self._lock(task, line, LockReason.BIN_DISABLED, f"药斗 {scanned_bin} 已停用", at, operator_id)
            outcome = ScanOutcome(scan_id, line_id, False, "bin-disabled", lock)
        elif scanned_bin != line.expected_bin:
            lock = self._lock(
                task,
                line,
                LockReason.IDENTITY_MISMATCH,
                f"{line.line.display_name} 应扫 {line.expected_bin}，实扫 {scanned_bin}",
                at,
                operator_id,
            )
            outcome = ScanOutcome(scan_id, line_id, False, "identity-mismatch", lock)
        else:
            line.status = LineStatus.ACTIVE
            task.active_line_id = line_id
            outcome = ScanOutcome(scan_id, line_id, True, "active")
        self._scans[key] = outcome
        return outcome

    # -------------------------------------------------------------- 秤台事件

    def record_scale_event(self, task_id: str, event: ScaleEvent) -> ScaleEventOutcome:
        """接收秤台事件；只归入当前药味，违规事件留痕但不生效。"""
        if event.event_id in self._events:  # 幂等：重发返回首次结果
            return self._events[event.event_id]
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.OPEN, action="接收秤台事件")
        line = self._get_line(task, event.line_id)

        def finish(applied: bool, note: str, lock: LockRecord | None = None) -> ScaleEventOutcome:
            task.event_log.append(LoggedEvent(seq=len(task.event_log), event=event, applied=applied, note=note))
            outcome = ScaleEventOutcome(event.event_id, event.line_id, applied, note, lock)
            self._events[event.event_id] = outcome
            return outcome

        if line.status is LineStatus.LOCKED:
            return finish(False, "line-locked", line.lock)
        if line.status is not LineStatus.ACTIVE or task.active_line_id != line.line.line_id:
            return finish(False, "not-current-line")
        if event.bin_code != line.expected_bin:
            return finish(False, "bin-mismatch")
        if line.device_id is not None and event.device_id != line.device_id:
            return finish(False, "device-mismatch")
        if not event.calibration_valid:
            lock = self._lock(
                task,
                line,
                LockReason.CALIBRATION_EXPIRED,
                f"秤台 {event.device_id} 校准失效",
                event.occurred_at,
                actor_id=event.device_id,
            )
            return finish(False, "calibration-expired", lock)

        if event.action is ScaleAction.TARE:
            line.device_id = line.device_id or event.device_id
            line.tare_grams = event.reading_grams
            return finish(True, "tare")
        if event.action is ScaleAction.ADD:
            if line.tare_grams is None:
                return finish(False, "tare-required")
            line.device_id = line.device_id or event.device_id
            line.net_grams += event.reading_grams
            return finish(True, "add")
        if event.action is ScaleAction.REMOVE:
            if line.tare_grams is None:
                return finish(False, "tare-required")
            if event.reading_grams > line.net_grams:
                return finish(False, "remove-exceeds-net")
            line.device_id = line.device_id or event.device_id
            line.net_grams -= event.reading_grams
            return finish(True, "remove")
        if event.action is ScaleAction.ACCEPT:
            if line.tare_grams is None:
                return finish(False, "tare-required")
            if event.reading_grams != line.net_grams:
                return finish(False, "reading-mismatch")
            line.device_id = line.device_id or event.device_id
            target = line.line.target_grams
            tolerance = line.line.tolerance_grams
            if abs(line.net_grams - target) > tolerance:
                lock = self._lock(
                    task,
                    line,
                    LockReason.OUT_OF_TOLERANCE,
                    f"目标 {target}g±{tolerance}g，实称 {line.net_grams}g",
                    event.occurred_at,
                    actor_id=event.device_id,
                )
                return finish(False, "out-of-tolerance", lock)
            line.status = LineStatus.ACCEPTED
            line.accepted_grams = line.net_grams
            line.accepted_device_id = line.device_id
            line.accepted_calibration_valid = True
            line.accept_event_id = event.event_id
            task.active_line_id = None
            return finish(True, "accepted")
        raise ValueError(f"未知秤台动作: {event.action}")

    def switch_scale(
        self,
        task_id: str,
        line_id: str,
        to_device_id: str,
        *,
        at: datetime,
        operator_id: str | None = None,
    ) -> None:
        """换秤：旧秤去皮值立即作废，新秤必须重新去皮。"""
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.OPEN, action="换秤")
        line = self._get_line(task, line_id)
        if line.status is not LineStatus.ACTIVE:
            raise InvalidStateError(f"药味 {line_id} 状态为 {line.status}，不能换秤")
        if line.device_id == to_device_id:
            raise InvalidStateError(f"药味 {line_id} 已绑定秤台 {to_device_id}")
        old = line.device_id
        line.device_id = to_device_id
        line.tare_grams = None
        task.audit.append(
            AuditEntry(at=at, kind="switch-scale", detail=f"{line.line.display_name}: {old} -> {to_device_id}，旧去皮值作废", actor_id=operator_id)
        )

    # ------------------------------------------------------------------ 锁定

    def unlock_line(
        self,
        task_id: str,
        line_id: str,
        *,
        supervisor_id: str,
        note: str,
        at: datetime,
    ) -> LockRecord:
        """组长解锁：药味回到待扫码重新称量，锁定记录永久保留。"""
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.OPEN, action="解锁")
        line = self._get_line(task, line_id)
        if line.status is not LineStatus.LOCKED or line.lock is None:
            raise InvalidStateError(f"药味 {line_id} 未处于锁定状态")
        record = line.lock
        record.unlocked_by = supervisor_id
        record.unlocked_at = at
        record.unlock_note = note
        line.status = LineStatus.PENDING
        line.device_id = None
        line.tare_grams = None
        line.net_grams = Decimal("0")
        line.lock = None
        task.audit.append(
            AuditEntry(at=at, kind="unlock", detail=f"{line.line.display_name}: {note}", actor_id=supervisor_id)
        )
        return record

    # -------------------------------------------------------------- 封袋复核

    def seal_package(
        self,
        task_id: str,
        *,
        seal: str,
        at: datetime,
        operator_id: str | None = None,
    ) -> DispensingTask:
        """全部药味合格后封袋；同一封袋号重复操作幂等。"""
        task = self._get_task(task_id)
        if task.status is TaskStatus.PACKAGED and task.package_seal == seal:
            return task
        self._require_status(task, TaskStatus.OPEN, TaskStatus.RETURNED, action="封袋")
        not_ready = [lid for lid, ls in task.lines.items() if ls.status is not LineStatus.ACCEPTED]
        if not_ready:
            raise InvalidStateError(f"药味未全部合格，不能封袋: {not_ready}")
        task.status = TaskStatus.PACKAGED
        task.package_seal = seal
        task.audit.append(AuditEntry(at=at, kind="seal", detail=f"封袋 {seal}", actor_id=operator_id))
        return task

    def submit_review(
        self,
        task_id: str,
        review_id: str,
        reviewer_id: str,
        *,
        approve: bool,
        at: datetime,
    ) -> DispensingReview:
        """双人复核：复核员不得是调剂员本人。"""
        if review_id in self._reviews:  # 幂等
            return self._reviews[review_id]
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.PACKAGED, action="复核")
        if reviewer_id == task.dispenser_id:
            raise SelfReviewError(f"复核员 {reviewer_id} 不得复核自己的称量")
        review = DispensingReview(
            review_id=review_id,
            session_id=task.task_id,
            dispenser_id=task.dispenser_id,
            reviewer_id=reviewer_id,
            package_seal=task.package_seal,
            reviewed_at=at,
        )
        task.reviews.append(review)
        task.status = TaskStatus.COMPLETED if approve else TaskStatus.RETURNED
        task.audit.append(
            AuditEntry(at=at, kind="review", detail="复核通过" if approve else "复核退回", actor_id=reviewer_id)
        )
        self._reviews[review_id] = review
        return review

    # -------------------------------------------------------------- 处方变更

    def apply_prescription_change(
        self,
        superseded_snapshot_id: str,
        new_snapshot_id: str,
        *,
        at: datetime,
        operator_id: str | None = None,
    ) -> list[str]:
        """处方变更：未包装任务作废，已封袋任务只能进入拆包复核。"""
        affected: list[str] = []
        for task in self._tasks.values():
            if task.snapshot_id != superseded_snapshot_id:
                continue
            if task.status in (TaskStatus.OPEN, TaskStatus.RETURNED):
                task.status = TaskStatus.VOIDED
                task.audit.append(
                    AuditEntry(at=at, kind="void", detail=f"处方快照 {superseded_snapshot_id} 被 {new_snapshot_id} 取代，未包装任务作废", actor_id=operator_id)
                )
                affected.append(task.task_id)
            elif task.status is TaskStatus.PACKAGED:
                task.status = TaskStatus.UNSEAL_REVIEW
                task.audit.append(
                    AuditEntry(at=at, kind="unseal-review", detail=f"处方快照 {superseded_snapshot_id} 被 {new_snapshot_id} 取代，已封袋转拆包复核", actor_id=operator_id)
                )
                affected.append(task.task_id)
        return affected

    def complete_unseal_review(
        self,
        task_id: str,
        reviewer_id: str,
        *,
        decision: str,
        at: datetime,
        note: str = "",
    ) -> DispensingTask:
        """拆包复核结论：void 作废重配，reseal 维持封袋。"""
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.UNSEAL_REVIEW, action="拆包复核")
        if reviewer_id == task.dispenser_id:
            raise SelfReviewError(f"复核员 {reviewer_id} 不得复核自己的称量")
        if decision == "void":
            task.status = TaskStatus.VOIDED
        elif decision == "reseal":
            task.status = TaskStatus.PACKAGED
        else:
            raise ValueError(f"未知拆包复核结论: {decision}")
        task.audit.append(
            AuditEntry(at=at, kind="unseal-done", detail=f"{decision}: {note}", actor_id=reviewer_id)
        )
        return task

    # -------------------------------------------------------------- 调剂单

    def qualified_slip(self, task_id: str) -> DispensingSlip:
        """生成合格调剂单；每个任务只生成一份，重复调用返回同一份。"""
        task = self._get_task(task_id)
        self._require_status(task, TaskStatus.COMPLETED, action="开具调剂单")
        if task.slip is None:
            review = task.reviews[-1]
            slip_lines = []
            for ls in task.lines.values():
                assert ls.accepted_grams is not None
                slip_lines.append(
                    SlipLine(
                        line_id=ls.line.line_id,
                        display_name=ls.line.display_name,
                        target_grams=ls.line.target_grams,
                        actual_grams=ls.accepted_grams,
                        device_id=ls.accepted_device_id or "",
                        calibration_valid=bool(ls.accepted_calibration_valid),
                        accept_event_id=ls.accept_event_id or "",
                    )
                )
            task.slip = DispensingSlip(
                slip_id=f"slip-{task.task_id}",
                session_id=task.task_id,
                snapshot_id=task.snapshot_id,
                package_seal=task.package_seal or "",
                lines=tuple(slip_lines),
                dispenser_id=task.dispenser_id,
                reviewer_id=review.reviewer_id,
                issued_at=review.reviewed_at,
            )
        return task.slip

    def qualified_slips(self) -> list[DispensingSlip]:
        """全系统已开具的合格调剂单。"""
        return [
            self.qualified_slip(task.task_id)
            for task in self._tasks.values()
            if task.status is TaskStatus.COMPLETED
        ]

    # ------------------------------------------------------------------ 交班

    def handover_report(self, task_id: str | None = None) -> list[HandoverEntry]:
        """交班报告：全部锁定记录（含已解决的），原因永久可查。"""
        tasks = [self._get_task(task_id)] if task_id is not None else list(self._tasks.values())
        entries: list[HandoverEntry] = []
        for task in tasks:
            for record in task.lock_history:
                line = task.lines[record.line_id]
                entries.append(
                    HandoverEntry(
                        task_id=task.task_id,
                        task_status=task.status,
                        line_id=record.line_id,
                        display_name=line.line.display_name,
                        reason=record.reason,
                        detail=record.detail,
                        locked_at=record.locked_at,
                        resolved=record.resolved,
                        unlocked_by=record.unlocked_by,
                    )
                )
        entries.sort(key=lambda entry: entry.locked_at)
        return entries

    # ------------------------------------------------------------------ 内部

    def _get_task(self, task_id: str) -> DispensingTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise UnknownTaskError(f"任务 {task_id} 不存在") from None

    @staticmethod
    def _get_line(task: DispensingTask, line_id: str) -> LineState:
        try:
            return task.lines[line_id]
        except KeyError:
            raise UnknownLineError(f"任务 {task.task_id} 没有药味 {line_id}") from None

    @staticmethod
    def _require_status(task: DispensingTask, *allowed: TaskStatus, action: str) -> None:
        if task.status not in allowed:
            names = "/".join(status.value for status in allowed)
            raise InvalidStateError(f"任务 {task.task_id} 状态为 {task.status}，不能{action}（需要 {names}）")

    @staticmethod
    def _lock(
        task: DispensingTask,
        line: LineState,
        reason: LockReason,
        detail: str,
        at: datetime,
        actor_id: str | None,
    ) -> LockRecord:
        record = LockRecord(
            line_id=line.line.line_id, reason=reason, detail=detail, locked_at=at, actor_id=actor_id
        )
        line.status = LineStatus.LOCKED
        line.lock = record
        task.lock_history.append(record)
        if task.active_line_id == line.line.line_id:
            task.active_line_id = None
        task.audit.append(
            AuditEntry(at=at, kind="lock", detail=f"{line.line.display_name}: {reason.value} — {detail}", actor_id=actor_id)
        )
        return record
