"""请求体的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from .models import UtcInstant


def _parse_instant(value):
    if isinstance(value, UtcInstant):
        return value
    if isinstance(value, str):
        return UtcInstant.parse(value)
    if isinstance(value, datetime):
        # 兼容直接传 datetime 的调用方（精度只到微秒）。
        return UtcInstant.from_datetime(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 兼容 Unix 时间戳（秒，可为小数；超出微秒的部分同样保留）。
        seconds = int(value)
        nanos = round((value - seconds) * 1_000_000_000)
        if nanos >= 1_000_000_000:
            seconds, nanos = seconds + 1, nanos - 1_000_000_000
        if nanos < 0:
            seconds, nanos = seconds - 1, nanos + 1_000_000_000
        return UtcInstant(seconds * 1_000_000_000 + nanos)
    raise ValueError("sampled_at 必须是 ISO8601 字符串或 Unix 时间戳")


# 用自定义类型解析，避免 Pydantic 把 datetime 截断到微秒。
SampledAt = Annotated[
    UtcInstant,
    BeforeValidator(_parse_instant),
    WithJsonSchema(
        {"type": "string", "format": "date-time", "description": "采样时间，ISO8601（最多 9 位小数秒）"}
    ),
]


class LivenessReportIn(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_id: str = Field(min_length=1, max_length=128)
    sampled_at: SampledAt
    healthy: bool
    boot_id: str = Field(min_length=1, max_length=128, description="启动标识")


class LivenessBatchIn(BaseModel):
    reports: list[LivenessReportIn] = Field(min_length=1, max_length=5000)


class PermitRequestIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128, description="申请号")
    nodes: list[str] = Field(min_length=1, max_length=5000, description="拟重启节点")
