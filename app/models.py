"""领域模型与时间工具。

探活采样时间与许可审批/结束时间统一表示为 :class:`UtcInstant`，
以“Unix 纪元以来的纳秒数”保存，完整覆盖 ISO8601 最多 9 位小数秒。
Python 自带的 ``datetime`` 只有微秒精度，不能用于比较这类时间。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NS_PER_SECOND = 1_000_000_000

# 允许：日期 + T/空格 + 时:分[:秒[.小数]] + 可选时区（缺省按 UTC）
_ISO_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt ](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:[.,](?P<fraction>\d+))?)?"
    r"(?P<tz>[Zz]|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True, order=True)
class UtcInstant:
    """UTC 时刻，精确到纳秒；``nanos`` 是 Unix 纪元以来的纳秒数。"""

    nanos: int

    @classmethod
    def now(cls) -> "UtcInstant":
        """当前 UTC 时刻。使用 ``time.time_ns()``，不经过微秒精度的 datetime。"""
        return cls(time.time_ns())

    @classmethod
    def from_datetime(cls, dt: datetime) -> "UtcInstant":
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt.astimezone(timezone.utc) - _EPOCH
        return cls(
            (delta.days * 86400 + delta.seconds) * _NS_PER_SECOND
            + delta.microseconds * 1000
        )

    @classmethod
    def parse(cls, value: str) -> "UtcInstant":
        """解析 ISO8601 时间，保留最多 9 位小数秒。"""
        if not isinstance(value, str):
            raise ValueError("时间必须是 ISO8601 字符串")
        text = value.strip()
        m = _ISO_RE.match(text)
        if m is None:
            # 兼容 Python 能解析的其他 ISO 变体（此时只有微秒精度）。
            try:
                return cls.from_datetime(datetime.fromisoformat(text))
            except ValueError as exc:
                raise ValueError(f"无法解析 ISO8601 时间: {value!r}") from exc

        fraction = m.group("fraction") or ""
        # ISO8601 小数秒最多 9 位；超出部分无法表示，按截断处理。
        frac_ns = int((fraction + "000000000")[:9]) if fraction else 0

        tz_text = m.group("tz")
        if tz_text is None or tz_text in ("Z", "z"):
            tz = timezone.utc
        else:
            sign = 1 if tz_text[0] == "+" else -1
            digits = tz_text[1:].replace(":", "")
            offset = timedelta(
                hours=int(digits[:2]), minutes=int(digits[2:4])
            )
            tz = timezone(sign * offset)

        try:
            base = datetime(
                int(m.group("year")),
                int(m.group("month")),
                int(m.group("day")),
                int(m.group("hour")),
                int(m.group("minute")),
                int(m.group("second") or 0),
                tzinfo=tz,
            )
        except ValueError as exc:
            raise ValueError(f"无法解析 ISO8601 时间: {value!r}") from exc

        delta = base.astimezone(timezone.utc) - _EPOCH
        base_ns = (delta.days * 86400 + delta.seconds) * _NS_PER_SECOND
        return cls(base_ns + frac_ns)

    def to_iso(self) -> str:
        """UTC ISO8601 文本，保留全部有效小数秒（最多 9 位）。"""
        seconds, nanos = divmod(self.nanos, _NS_PER_SECOND)
        dt = datetime.fromtimestamp(seconds, timezone.utc)
        text = (
            f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        )
        if nanos:
            text += "." + f"{nanos:09d}".rstrip("0")
        return text + "+00:00"

    def __str__(self) -> str:  # pragma: no cover - 便于日志/调试
        return self.to_iso()


def now_utc() -> UtcInstant:
    return UtcInstant.now()


def to_iso(value: UtcInstant | datetime) -> str:
    if isinstance(value, UtcInstant):
        return value.to_iso()
    return UtcInstant.from_datetime(value).to_iso()


def parse_iso(value: str) -> UtcInstant:
    return UtcInstant.parse(value)


@dataclass(frozen=True)
class LatestReport:
    """某节点采样时刻最新的一份探活报告。"""

    node_id: str
    sampled_at: UtcInstant
    healthy: bool
    boot_id: str


@dataclass(frozen=True)
class PermitRecord:
    """一条许可。status: open（未结束）| finished（已结束）。"""

    request_id: str
    nodes: tuple[str, ...]  # 有序、去重
    status: str
    approved_at: UtcInstant
    finished_at: UtcInstant | None
    boot_ids_at_approval: dict[str, str]  # 获批时各节点最新报告中的启动标识
