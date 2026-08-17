"""Dataplane-local integer enumerations for hot-path array indexing.

These mirror the control-plane enums but use IntEnum exclusively so they
can be used directly as array indices with zero overhead.
"""

from enum import IntEnum


class ModeId(IntEnum):
    AUTO = 0
    FAST = 1
    EXPERT = 2
    HEAVY = 3
    GROK_4_3 = 4
    CONSOLE = 5  # console.x.ai 独立配额
    IMAGE_PRO = 6  # Web Imagine imagePro quota fence
    IMAGE_EDIT = 7  # Web Imagine imageEdit quota fence
    VIDEO = 8  # Web Imagine video quota fence
    VIDEO_720P = 9  # Web Imagine video720p quota fence


class PoolId(IntEnum):
    BASIC = 0
    SUPER = 1
    HEAVY = 2


class StatusId(IntEnum):
    ACTIVE = 0
    COOLING = 1
    EXPIRED = 2
    DISABLED = 3
    DELETED = 4


# Map pool string → PoolId integer (used during sync from control plane).
POOL_STR_TO_ID: dict[str, int] = {
    "basic": int(PoolId.BASIC),
    "super": int(PoolId.SUPER),
    "heavy": int(PoolId.HEAVY),
}

POOL_ID_TO_STR: dict[int, str] = {v: k for k, v in POOL_STR_TO_ID.items()}

STATUS_STR_TO_ID: dict[str, int] = {
    "active": int(StatusId.ACTIVE),
    "cooling": int(StatusId.COOLING),
    "expired": int(StatusId.EXPIRED),
    "disabled": int(StatusId.DISABLED),
}

ALL_MODE_IDS: tuple[int, ...] = (
    int(ModeId.AUTO),
    int(ModeId.FAST),
    int(ModeId.EXPERT),
    int(ModeId.HEAVY),
    int(ModeId.GROK_4_3),
    int(ModeId.CONSOLE),
    int(ModeId.IMAGE_PRO),
    int(ModeId.IMAGE_EDIT),
    int(ModeId.VIDEO),
    int(ModeId.VIDEO_720P),
)

IMAGE_QUOTA_MODE_IDS: tuple[int, ...] = (
    int(ModeId.IMAGE_PRO),
    int(ModeId.IMAGE_EDIT),
)

VIDEO_QUOTA_MODE_IDS: tuple[int, ...] = (
    int(ModeId.VIDEO),
    int(ModeId.VIDEO_720P),
)

IMAGINE_QUOTA_MODE_IDS: tuple[int, ...] = IMAGE_QUOTA_MODE_IDS + VIDEO_QUOTA_MODE_IDS

__all__ = [
    "ModeId",
    "PoolId",
    "StatusId",
    "POOL_STR_TO_ID",
    "POOL_ID_TO_STR",
    "STATUS_STR_TO_ID",
    "ALL_MODE_IDS",
    "IMAGE_QUOTA_MODE_IDS",
    "IMAGINE_QUOTA_MODE_IDS",
    "VIDEO_QUOTA_MODE_IDS",
]
