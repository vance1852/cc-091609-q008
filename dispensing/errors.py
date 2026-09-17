"""调剂防错领域异常。"""

from __future__ import annotations


class DomainError(Exception):
    """调剂领域错误基类。"""


class UnknownTaskError(DomainError):
    """任务不存在。"""


class UnknownLineError(DomainError):
    """处方药味不存在。"""


class DuplicateTaskError(DomainError):
    """任务编号重复。"""


class InvalidStateError(DomainError):
    """当前任务或药味状态不允许该操作。"""


class SelfReviewError(DomainError):
    """复核员不得复核自己的称量。"""
