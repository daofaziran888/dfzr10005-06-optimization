"""成本扫描取数源：把分析范围拆成按「表 + 分区」的扫描分段。

这里用一个确定性的模拟数仓目录代替真实对象存储：
- 同一个范围每次返回相同的表/分区清单，便于断点续扫与缓存复用；
- 采样版本只影响被新采样覆盖到的分区，未受影响的分区可以命中上一次结果；
- 指定分区的首次读取会模拟一次「目录不可读」，用于验证失败隔离与重试恢复。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

DECIMAL_GB = 1_000_000_000

KNOWN_SCOPE = "数仓明细层 dwd_*、汇总层 dws_* 与归档层 ods_*（含冷分区）"
KNOWN_SCOPE_FINGERPRINT = ""

DEFAULT_TABLES = (
    "dwd_order_detail",
    "dwd_user_profile",
    "dws_trade_summary",
    "ods_event_log",
    "dim_customer",
    "ads_cost_dashboard",
    "dwd_payment_flow",
    "dwd_refund_detail",
)


def normalize_scope(scope: str) -> str:
    """折叠空白，保证同一范围的重复提交落到同一个范围指纹。"""
    return " ".join(str(scope or "").split())


def scope_fingerprint(scope: str) -> str:
    return hashlib.sha256(normalize_scope(scope).encode("utf-8")).hexdigest()[:12]


# 已知范围的基础清单：表名、分区、存储量（GB）、最近访问距今天数。
_KNOWN_SEGMENTS: tuple[tuple[str, str, int, int], ...] = (
    ("dwd_order_detail", "dt=2026-09-01", 8400, 5),
    ("dwd_order_detail", "dt=2026-09-02", 8650, 3),
    ("dwd_order_detail", "dt=2026-09-03", 8900, 1),
    ("dwd_user_profile", "dt=2026-08-01", 3200, 82),
    ("dwd_user_profile", "dt=2026-09-01", 3450, 9),
    ("dws_trade_summary", "dt=2026-08-01", 6400, 41),
    ("dws_trade_summary", "dt=2026-09-01", 6750, 2),
    ("dim_customer", "dt=2026-01-01", 120, 65),
    ("ods_event_log", "dt=2026-05-01", 5200, 120),
    ("ods_event_log", "dt=2026-06-01", 5600, 90),
    ("ods_event_log", "dt=2026-07-01", 5900, 60),
    ("ads_cost_dashboard", "dt=2026-09-01", 2100, 4),
)

# 采样轮次会覆盖到的分段：第 2 轮覆盖两个 ODS 冷分区，第 3 轮覆盖一个明细冷分区。
_KNOWN_SAMPLING_ROUNDS: dict[int, set[int]] = {
    2: {8, 9},
    3: {3},
}

# 首次读取会失败的分段：模拟对象存储授权过期，重试（第 2 次读取）即可恢复。
_KNOWN_FAULT_PATH = "s3://warehouse/ods/event_log/dt=2026-05-01"


class SourceReadError(Exception):
    """分区不可读：带上可操作的失败代码与处理建议。"""

    def __init__(
        self,
        *,
        code: str,
        reason: str,
        suggestion: str,
        retryable: bool,
        path: str,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.suggestion = suggestion
        self.retryable = retryable
        self.path = path


@dataclass(frozen=True)
class InventorySegment:
    """一个可独立重试的扫描分段；只暴露取数需要的元数据。"""

    table: str
    partition: str
    path: str
    storage_bytes: int
    row_count: int
    recent_access_days: int
    sample_round: int


def _layer_path(table: str, partition: str) -> str:
    layer = table.split("_", 1)[0]
    short = table[len(layer) + 1 :]
    return f"s3://warehouse/{layer}/{short}/{partition}"


def _known_inventory(scope: str, sampling_version: int) -> list[InventorySegment]:
    fingerprint = scope_fingerprint(scope)
    segments: list[InventorySegment] = []
    for index, (table, partition, size_gb, recent_days) in enumerate(_KNOWN_SEGMENTS):
        round_no = 1
        for candidate_round, covered in _KNOWN_SAMPLING_ROUNDS.items():
            if sampling_version >= candidate_round and index in covered:
                round_no = candidate_round
        # 被新采样覆盖的分区，存储量与访问时间会随轮次发生变化，才有重算意义。
        factor = 1 + 0.015 * (round_no - 1)
        storage_bytes = int(size_gb * DECIMAL_GB * factor)
        adjusted_days = max(0, recent_days - (round_no - 1) * 2)
        row_count = max(1, int(storage_bytes / (520 + (index % 5) * 24)))
        path = _layer_path(table, partition)
        # 范围指纹参与路径哈希，避免不同范围的缓存互相污染。
        path_digest = hashlib.sha256(f"{fingerprint}:{path}".encode()).hexdigest()
        if int(path_digest[:8], 16) % 1000 == 0:
            path = f"{path}/part-{path_digest[:4]}"
        segments.append(
            InventorySegment(
                table=table,
                partition=partition,
                path=path,
                storage_bytes=storage_bytes,
                row_count=row_count,
                recent_access_days=adjusted_days,
                sample_round=round_no,
            )
        )
    return segments


def _generic_inventory(scope: str, sampling_version: int) -> list[InventorySegment]:
    digest = hashlib.sha256(normalize_scope(scope).encode("utf-8")).digest()
    table_count = 6 + digest[0] % 4
    segments: list[InventorySegment] = []
    for index in range(table_count):
        table = DEFAULT_TABLES[(digest[index % len(digest)] + index) % len(DEFAULT_TABLES)]
        month = 5 + (digest[(index + 3) % len(digest)] % 5)
        partition = f"dt=2026-{month:02d}-01"
        base_gb = 700 + (int.from_bytes(digest[(index * 2) % len(digest):][:2], "big") % 8200)
        round_no = 1
        if sampling_version >= 2 and (index + digest[1]) % 4 == 0:
            round_no = 2
        if sampling_version >= 3 and (index + digest[2]) % 7 == 0:
            round_no = 3
        factor = 1 + 0.012 * (round_no - 1)
        storage_bytes = int(base_gb * DECIMAL_GB * factor)
        recent_days = (digest[(index + 5) % len(digest)] * 190) // 255
        row_count = max(1, int(storage_bytes / 640))
        path = _layer_path(table, partition)
        segments.append(
            InventorySegment(
                table=table,
                partition=partition,
                path=path,
                storage_bytes=storage_bytes,
                row_count=row_count,
                recent_access_days=recent_days,
                sample_round=round_no,
            )
        )
    return segments


def build_inventory(scope: str, sampling_version: int) -> list[InventorySegment]:
    """按范围与采样版本列出全部分段；空范围返回空清单，由上层给出空态提示。

    同一张表同一个分区只保留一个分段键：重复清单项会追加 part-N 后缀，
    否则并发扫描时两个分段写回同一个键，后完成的结果会被误判成他人写入。
    """
    normalized = normalize_scope(scope)
    if not normalized or len(normalized) < 4 or not re.search(r"[A-Za-z0-9_]", normalized):
        return []
    if normalized == normalize_scope(KNOWN_SCOPE):
        raw = _known_inventory(normalized, sampling_version)
    else:
        raw = _generic_inventory(normalized, sampling_version)
    seen: dict[tuple[str, str], int] = {}
    segments: list[InventorySegment] = []
    for segment in raw:
        marker = (segment.table, segment.partition)
        seen[marker] = seen.get(marker, 0) + 1
        path = segment.path
        if seen[marker] > 1:
            path = f"{path}/part-{seen[marker]:04d}"
        segments.append(
            InventorySegment(
                table=segment.table,
                partition=segment.partition,
                path=path,
                storage_bytes=segment.storage_bytes,
                row_count=segment.row_count,
                recent_access_days=segment.recent_access_days,
                sample_round=segment.sample_round,
            )
        )
    return segments


def read_segment(scope: str, segment: InventorySegment, attempt: int) -> dict[str, Any]:
    """读取一个分段。attempt 从 1 开始；首次命中模拟的不可读目录会抛出可重试错误。"""
    normalized = normalize_scope(scope)
    if (
        normalized == normalize_scope(KNOWN_SCOPE)
        and segment.path == _KNOWN_FAULT_PATH
        and attempt <= 1
    ):
        raise SourceReadError(
            code="PARTITION_READ_PERMISSION",
            reason=(
                f"分区 {segment.path} 不可读：对象存储返回 PermissionDenied(403)，"
                "读取账号 warehouse_reader 的路径授权已在 2026-11-12 过期。"
            ),
            suggestion=(
                "请存储管理员恢复该路径读权限后点击「重试失败段」；"
                "若该分区已归档下线，可跳过此段，容量摘要会标注为部分覆盖。"
            ),
            retryable=True,
            path=segment.path,
        )
    return segment_result(segment)


def segment_result(segment: InventorySegment) -> dict[str, Any]:
    """把分段元数据转成取数结果；历史批次回放和真实读取共用同一结构。"""
    return {
        "表名称": segment.table,
        "分区": segment.partition,
        "数据源路径": segment.path,
        "存储字节": segment.storage_bytes,
        "行数": segment.row_count,
        "最近访问天数": segment.recent_access_days,
        "采样批次": segment.sample_round,
    }


def build_seed_batch(scope: str, sampling_version: int = 1) -> list[dict[str, Any]]:
    """构造一条已完成的历史分段清单，用来承接 BENC-0002 已引用的容量摘要。"""
    segments: list[dict[str, Any]] = []
    for segment in build_inventory(scope, sampling_version):
        segments.append(
            {
                "分段键": f"{segment.table}|{segment.partition}|{segment.path}",
                "表名称": segment.table,
                "分区": segment.partition,
                "数据源路径": segment.path,
                "存储字节": segment.storage_bytes,
                "行数": segment.row_count,
                "最近访问天数": segment.recent_access_days,
                "采样批次": segment.sample_round,
                "结果版本": segment.sample_round,
                "状态": "已完成",
                "尝试次数": 1,
                "认领时间": "",
                "结果": segment_result(segment),
                "上次结果": None,
                "失败原因": {},
                "完成时间": "2026-09-02 10:20:00",
            }
        )
    return segments
