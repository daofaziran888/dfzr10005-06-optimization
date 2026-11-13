"""成本扫描编排：按表/分区切段、断点续扫、失败隔离与容量摘要发布。

数据流：
    POST /api/cost/scans
      → CostScanService.create_scan(范围, 采样批次, 报告编号)
      → build_inventory() 列出可独立重试的「表 + 分区」分段
      → 逐段 read_segment()，逐段落状态（已完成 / 失败 / 待扫描）
      → 全部成功段汇总成容量摘要，回写绑定报告的成本字段
      → 查询性能报告按「容量摘要编号」引用同一份扫描范围

关键约束：
- 同一范围指纹只保留一个批次；新采样只把受影响分段打回待扫描，其余命中缓存；
- 已完成分段不会被重试或续扫覆盖；
- 分段失败只影响该段，报告不会整份失败；
- 容量摘要只发布一次，并发结束同一批次时不会重复写入预计节省。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.services.cost_source import (
    InventorySegment,
    SourceReadError,
    build_inventory,
    normalize_scope,
    read_segment,
    scope_fingerprint,
)
from app.store import store

MODULE = "cost"
COLD_DAYS_THRESHOLD = 60
SAVING_RATIO = 0.35
STALE_CLAIM_SECONDS = 60
STATUS_PENDING = "待扫描"
STATUS_RUNNING = "扫描中"
STATUS_DONE = "已完成"
STATUS_FAILED = "失败"
STATUS_INTERRUPTED = "已中断"
STATUS_PARTIAL = "部分失败"
STATUS_COMPLETE = "已完成"
STATUS_EMPTY = "暂无数据"

_LOCK = threading.RLock()
_CST = timezone(timedelta(hours=8))


def now_text() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")


def format_tb(byte_count: int) -> str:
    """十进制 TB，和示例报告里的 64.67 TB 口径一致。"""
    return f"{byte_count / 1_000_000_000_000:.2f} TB"


def format_percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "0.0%"
    return f"{part / whole * 100:.1f}%"


def _segment_key(segment: InventorySegment) -> str:
    return f"{segment.table}|{segment.partition}|{segment.path}"


def _public_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """对外只暴露统计与分段状态，分段结果单独在明细里给出。"""
    return deepcopy(batch)


class CostScanService:
    # ---------- 查询 ----------

    def list_scans(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _LOCK:
            rows = [deepcopy(row) for row in store.scan_batches()]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("批次编号", ""))
                or keyword in str(row.get("分析范围", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: row.get("序号", 0), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_scan(self, batch_id: str) -> dict[str, Any] | None:
        with _LOCK:
            row = self._find_batch(batch_id)
            return deepcopy(row) if row else None

    def get_scan_for_scope(self, scope: str) -> dict[str, Any] | None:
        fingerprint = scope_fingerprint(scope)
        with _LOCK:
            row = self._find_by_fingerprint(fingerprint)
            return deepcopy(row) if row else None

    def list_capacity_summaries(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _LOCK:
            rows = [deepcopy(row) for row in store.capacity_summaries()]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("摘要编号", ""))
                or keyword in str(row.get("扫描范围", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_capacity_summary(self, summary_id: str) -> dict[str, Any] | None:
        with _LOCK:
            row = store.capacity_summary(summary_id)
            return deepcopy(row) if row else None

    # ---------- 批次创建 / 续扫 / 重试 ----------

    def create_scan(
        self,
        *,
        scope: str,
        sampling_version: int = 1,
        report_id: int | None = None,
        max_segments: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        scope_text = normalize_scope(scope)
        if not scope_text:
            return None, "分析范围为空，请先填写要扫描的表或分区范围（例如 dwd_* 2026-09 分区）"
        if sampling_version < 1:
            return None, "采样批次必须是大于等于 1 的整数"

        fingerprint = scope_fingerprint(scope_text)
        with _LOCK:
            batch = self._find_by_fingerprint(fingerprint)
            created = batch is None
            if batch is None:
                batch = self._new_batch_locked(scope_text, fingerprint, sampling_version)
                batch["报告编号"] = None
                batch["发布版本"] = 0
                mode = "首次扫描"
                affected = [seg["分段键"] for seg in batch["分段"]]
            else:
                mode = "相同范围重复提交：复用已有计算批次"

            bound_report = None
            if report_id is not None:
                bound_report = store.find(MODULE, report_id)
                if bound_report is None:
                    return None, f"成本分析报告 {report_id} 不存在或已归档"
                if batch.get("报告编号") and batch["报告编号"] != bound_report.get("报告编号"):
                    return None, (
                        f"批次 {batch['批次编号']} 已绑定报告 {batch['报告编号']}，"
                        f"不能再绑定 {bound_report.get('报告编号')}"
                    )
                report_scope = normalize_scope(str(bound_report.get("分析范围") or ""))
                batch_scope_is_baseline = batch.get("分析范围") == report_scope
                if report_scope and not batch_scope_is_baseline and batch.get("已发布摘要"):
                    return None, (
                        f"分析范围与报告 {bound_report.get('报告编号')} 不一致："
                        f"报告为「{report_scope}」，本次为「{scope_text}」；"
                        "已发布的容量摘要不能跨范围覆盖，请按报告范围重新提交"
                    )
                batch["报告编号"] = bound_report.get("报告编号")

            if not created:
                affected = self._sync_inventory_locked(batch, sampling_version)
            if not batch.get("分段"):
                # 空范围不登记空批次，避免列表里堆出一串没有分段的记录。
                if created:
                    store.scan_batches().remove(batch)
                return None, "该范围没有可扫描的表或分区（暂无数据），请确认范围是否写错"
            batch["本轮重算分段"] = affected
            batch["当前成本口径"] = self._caliber(scope_text, sampling_version)
            batch["更新时间"] = now_text()
            inventory_size = len(batch.get("分段", []))
            if created:
                mode = f"首次扫描，共切分 {inventory_size} 个分段"
            elif affected:
                if sampling_version > 1:
                    mode = (
                        f"{mode}；采样批次 {sampling_version} 影响 {len(affected)} 个分段，"
                        "其余成功段命中缓存"
                    )
                else:
                    mode = f"{mode}；复用 {inventory_size} 个已完成分段，无需重算"

        ran = 0
        if affected or any(seg["状态"] == STATUS_PENDING for seg in batch.get("分段", [])):
            ran = self._run_locked_scan(batch, eligible={STATUS_PENDING}, max_segments=max_segments)
        with _LOCK:
            batch = self._find_batch(batch["批次编号"])
            assert batch is not None
            if batch.get("状态") == STATUS_EMPTY:
                return None, "该范围没有可扫描的表或分区（暂无数据），请确认范围是否写错"
            summary = batch.get("已发布摘要")
            progress = batch.get("进度", {})
            if summary and batch.get("状态") == STATUS_COMPLETE:
                message = (
                    f"{mode}；扫描完成，{progress.get('已完成', 0)} 个分段成功，"
                    f"已生成容量摘要 {summary}"
                )
            elif batch.get("状态") == STATUS_COMPLETE:
                message = f"{mode}；扫描完成，{progress.get('已完成', 0)} 个分段成功"
            elif batch.get("状态") == STATUS_PARTIAL:
                failed = batch.get("失败数", 0)
                message = (
                    f"{mode}；{ran} 个分段本轮执行，失败 {failed} 个，其余成功段已保留。"
                    "失败原因见对应分段，修复后点击「重试失败段」即可继续"
                )
            elif batch.get("状态") == STATUS_INTERRUPTED:
                message = (
                    f"{mode}；本轮执行 {ran} 个分段后按上限中断，"
                    f"仍有 {progress.get('待扫描', 0)} 个分段待扫描，点击「继续扫描」可从断点恢复"
                )
            else:
                message = f"{mode}；本轮执行 {ran} 个分段"
            return deepcopy(batch), message

    def resume_scan(
        self,
        batch_id: str,
        *,
        max_segments: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            batch = self._find_batch(batch_id)
            if batch is None:
                return None, f"扫描批次 {batch_id} 不存在"
            self._reclaim_stale_locked(batch)
            pending = sum(1 for seg in batch["分段"] if seg["状态"] == STATUS_PENDING)
            if pending == 0:
                failed = sum(1 for seg in batch["分段"] if seg["状态"] == STATUS_FAILED)
                if failed:
                    return deepcopy(batch), (
                        f"当前批次没有待扫描分段；{failed} 个失败段请点击「重试失败段」，"
                        "失败原因见分段明细"
                    )
                return deepcopy(batch), "当前批次没有待扫描分段，无需继续"
            batch["中断原因"] = ""
        ran = self._run_locked_scan(batch, eligible={STATUS_PENDING}, max_segments=max_segments)
        with _LOCK:
            batch = self._find_batch(batch_id)
            assert batch is not None
            stale = self._reclaim_stale_locked(batch)
            if batch["状态"] == STATUS_PARTIAL:
                message = f"续扫执行 {ran} 个分段，出现 {batch['失败数']} 个失败段，原因见分段明细"
            elif batch["状态"] == STATUS_INTERRUPTED:
                message = (
                    f"续扫执行 {ran} 个分段后再次达到上限，"
                    f"剩余 {batch['进度']['待扫描']} 个分段可继续"
                )
            elif batch["状态"] == STATUS_RUNNING:
                message = (
                    "续扫执行受限：有分段仍在其他请求的认领期内，"
                    "请稍后刷新进度；超时未完成的认领会自动回到待扫描"
                )
            else:
                message = f"续扫执行 {ran} 个分段，扫描完成"
            return deepcopy(batch), message

    def retry_failed(
        self,
        batch_id: str,
        *,
        max_segments: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            batch = self._find_batch(batch_id)
            if batch is None:
                return None, f"扫描批次 {batch_id} 不存在"
            failed = [seg for seg in batch["分段"] if seg["状态"] == STATUS_FAILED]
            if not failed:
                return deepcopy(batch), "当前批次没有失败分段，无需重试"
            blocked = [
                seg
                for seg in failed
                if not (seg.get("失败原因") or {}).get("可重试", False)
            ]
            retryable_ids = {
                seg["分段键"] for seg in failed if seg not in blocked
            }
            for seg in failed:
                if seg["分段键"] in retryable_ids:
                    seg["状态"] = STATUS_PENDING
                    seg["失败原因"] = {}
            batch["中断原因"] = ""
            batch["更新时间"] = now_text()
        if not retryable_ids:
            with _LOCK:
                return (
                    deepcopy(self._find_batch(batch_id)),
                    "失败分段均为不可重试错误，请按失败原因处理后重新提交范围",
                )
        ran = self._run_locked_scan(batch, eligible={STATUS_PENDING}, max_segments=max_segments)
        with _LOCK:
            batch = self._find_batch(batch_id)
            assert batch is not None
            if batch["状态"] == STATUS_COMPLETE:
                message = f"重试 {ran} 个失败段全部成功，已发布容量摘要 {batch.get('已发布摘要')}"
            elif batch["状态"] == STATUS_PARTIAL:
                message = f"重试 {ran} 个分段后仍有 {batch['失败数']} 个失败段，请查看失败原因"
            else:
                message = f"重试执行 {ran} 个分段，当前状态 {batch['状态']}"
            return deepcopy(batch), message

    # ---------- 分段执行 ----------

    def _run_locked_scan(
        self,
        batch: dict[str, Any],
        *,
        eligible: set[str],
        max_segments: int | None,
    ) -> int:
        batch_id = batch["批次编号"]
        ran = 0
        while True:
            with _LOCK:
                current = self._find_batch(batch_id)
                if current is None:
                    return ran
                self._reclaim_stale_locked(current)
                if max_segments is not None and ran >= max_segments:
                    remaining = sum(
                        1 for seg in current["分段"] if seg["状态"] == STATUS_PENDING
                    )
                    if remaining:
                        current["中断原因"] = (
                            f"本次执行达到分段上限 {max_segments} 个，"
                            f"剩余 {remaining} 个分段待扫描；可从断点继续"
                        )
                    self._refresh_status_locked(current)
                    return ran
                segment = next(
                    (seg for seg in current["分段"] if seg["状态"] in eligible),
                    None,
                )
                if segment is None:
                    self._refresh_status_locked(current)
                    return ran
                # 争抢同一结束段时，只有第一个请求能把分段从待扫描改成扫描中。
                segment["状态"] = STATUS_RUNNING
                segment["认领时间"] = now_text()
                claim_token = uuid4().hex
                segment["认领标识"] = claim_token
                segment["尝试次数"] = int(segment.get("尝试次数", 0)) + 1
                scope = current["分析范围"]
                inventory = InventorySegment(
                    table=segment["表名称"],
                    partition=segment["分区"],
                    path=segment["数据源路径"],
                    storage_bytes=int(segment.get("存储字节") or 0),
                    row_count=int(segment.get("行数") or 0),
                    recent_access_days=int(segment.get("最近访问天数") or 0),
                    sample_round=int(segment.get("采样批次") or 1),
                )
                attempt = segment["尝试次数"]
                current["状态"] = STATUS_RUNNING
                current["更新时间"] = now_text()

            ran += 1
            try:
                result = read_segment(scope, inventory, attempt)
            except SourceReadError as error:
                with _LOCK:
                    current = self._find_batch(batch_id)
                    if current is None:
                        return ran
                    live = self._find_segment(current, segment["分段键"])
                    if live is None or not self._claim_is_current(live, claim_token):
                        continue
                    live["状态"] = STATUS_FAILED
                    live["失败原因"] = {
                        "错误码": error.code,
                        "原因": error.reason,
                        "处理建议": error.suggestion,
                        "可重试": error.retryable,
                        "数据源路径": error.path,
                        "失败时间": now_text(),
                    }
                    live["认领时间"] = ""
                    live["认领标识"] = ""
                    self._refresh_status_locked(current)
            except Exception as error:  # 取数层未预期异常也不能拖垮整份报告
                with _LOCK:
                    current = self._find_batch(batch_id)
                    if current is None:
                        return ran
                    live = self._find_segment(current, segment["分段键"])
                    if live is None or not self._claim_is_current(live, claim_token):
                        continue
                    live["状态"] = STATUS_FAILED
                    live["失败原因"] = {
                        "错误码": "SEGMENT_UNEXPECTED",
                        "原因": f"分段读取异常：{error}",
                        "处理建议": "稍后点击「重试失败段」；若持续失败请联系平台值班同学",
                        "可重试": True,
                        "数据源路径": live.get("数据源路径", ""),
                        "失败时间": now_text(),
                    }
                    live["认领时间"] = ""
                    live["认领标识"] = ""
                    self._refresh_status_locked(current)
            else:
                with _LOCK:
                    current = self._find_batch(batch_id)
                    if current is None:
                        return ran
                    live = self._find_segment(current, segment["分段键"])
                    if live is None or not self._claim_is_current(live, claim_token):
                        # 认领已被回收（例如并发请求判定超时），不覆盖别人写入的成功结果。
                        continue
                    live["结果"] = result
                    live["结果版本"] = int(segment.get("采样批次") or 1)
                    live["状态"] = STATUS_DONE
                    live["失败原因"] = {}
                    live["认领时间"] = ""
                    live["认领标识"] = ""
                    live["完成时间"] = now_text()
                    self._refresh_status_locked(current)

    # ---------- 批次与摘要 ----------

    def _new_batch_locked(
        self,
        scope: str,
        fingerprint: str,
        sampling_version: int,
    ) -> dict[str, Any]:
        inventory = build_inventory(scope, sampling_version)
        sequence = max(
            (int(row.get("序号", 0)) for row in store.scan_batches()), default=0
        ) + 1
        batch: dict[str, Any] = {
            "批次编号": f"SCAN-{sequence:04d}",
            "序号": sequence,
            "范围指纹": fingerprint,
            "分析范围": scope,
            "报告编号": None,
            "采样批次": sampling_version,
            "分段粒度": "表 + 分区（每个分区一个可重试分段）",
            "状态": STATUS_PENDING,
            "开始时间": now_text(),
            "更新时间": now_text(),
            "完成时间": "",
            "中断原因": "",
            "已发布摘要": "",
            "发布版本": 0,
            "汇总发布次数": 0,
            "当前成本口径": self._caliber(scope, sampling_version),
            "本轮重算分段": [],
            "分段": [
                {
                    "分段键": _segment_key(segment),
                    "表名称": segment.table,
                    "分区": segment.partition,
                    "数据源路径": segment.path,
                    "存储字节": segment.storage_bytes,
                    "行数": segment.row_count,
                    "最近访问天数": segment.recent_access_days,
                    "采样批次": segment.sample_round,
                    "结果版本": 0,
                    "状态": STATUS_PENDING,
                    "尝试次数": 0,
                    "认领时间": "",
                    "认领标识": "",
                    "结果": None,
                    "上次结果": None,
                    "失败原因": {},
                }
                for segment in inventory
            ],
        }
        store.scan_batches().append(batch)
        self._refresh_status_locked(batch)
        return batch

    def _sync_inventory_locked(
        self,
        batch: dict[str, Any],
        sampling_version: int,
    ) -> list[str]:
        """按新采样版本刷新分段元数据，只把受影响且已成功的分段打回待扫描。"""
        scope = batch["分析范围"]
        inventory = build_inventory(scope, sampling_version)
        batch["采样批次"] = sampling_version
        if not inventory:
            batch["分段"] = []
            self._refresh_status_locked(batch)
            return []

        existing = {seg["分段键"]: seg for seg in batch["分段"]}
        missing: list[dict[str, Any]] = []
        affected: list[str] = []
        seen: set[str] = set()
        for segment in inventory:
            key = _segment_key(segment)
            seen.add(key)
            live = existing.get(key)
            if live is None:
                live = {
                    "分段键": key,
                    "表名称": segment.table,
                    "分区": segment.partition,
                    "数据源路径": segment.path,
                    "存储字节": segment.storage_bytes,
                    "行数": segment.row_count,
                    "最近访问天数": segment.recent_access_days,
                    "采样批次": segment.sample_round,
                    "结果版本": 0,
                    "状态": STATUS_PENDING,
                    "尝试次数": 0,
                    "认领时间": "",
                    "认领标识": "",
                    "结果": None,
                    "上次结果": None,
                    "失败原因": {},
                }
                missing.append(live)
                affected.append(key)
                continue
            live["采样批次"] = segment.sample_round
            live["存储字节"] = segment.storage_bytes
            live["行数"] = segment.row_count
            live["最近访问天数"] = segment.recent_access_days
            if (
                live["状态"] == STATUS_DONE
                and int(live.get("结果版本") or 0) < segment.sample_round
            ):
                # 已成功但被新采样覆盖：保留上一次结果用于审计，重新排队测算。
                live["上次结果"] = live.get("结果")
                live["结果"] = None
                live["状态"] = STATUS_PENDING
                live["失败原因"] = {}
                affected.append(key)
            elif (
                live["状态"] == STATUS_FAILED
                and int(live.get("结果版本") or 0) < segment.sample_round
            ):
                # 新采样覆盖到失败段：重新排队，旧失败原因保留在 retry 前可查看的历史里。
                live["状态"] = STATUS_PENDING
                live["失败原因"] = {}
                affected.append(key)
        # 范围缩小时，已不存在的分段直接移除；它们从未计入任何已发布摘要。
        kept = [seg for seg in batch["分段"] if seg["分段键"] in seen]
        batch["分段"] = kept + missing
        self._refresh_status_locked(batch)
        return affected

    def _refresh_status_locked(self, batch: dict[str, Any]) -> None:
        segments = batch["分段"]
        total = len(segments)
        done = sum(1 for seg in segments if seg["状态"] == STATUS_DONE)
        failed = sum(1 for seg in segments if seg["状态"] == STATUS_FAILED)
        running = sum(1 for seg in segments if seg["状态"] == STATUS_RUNNING)
        pending = sum(1 for seg in segments if seg["状态"] == STATUS_PENDING)
        batch["分段总数"] = total
        batch["已完成数"] = done
        batch["失败数"] = failed
        batch["进度"] = {
            "总数": total,
            "已完成": done,
            "失败": failed,
            "扫描中": running,
            "待扫描": pending,
            "百分比": round((done + failed) / total * 100, 1) if total else 0.0,
        }
        if total == 0:
            batch["状态"] = STATUS_EMPTY
        elif running:
            batch["状态"] = STATUS_RUNNING
        elif failed and not pending:
            batch["状态"] = STATUS_PARTIAL
        elif pending:
            batch["状态"] = STATUS_INTERRUPTED if batch.get("中断原因") else STATUS_RUNNING
        else:
            batch["状态"] = STATUS_COMPLETE
            batch["中断原因"] = ""
            batch["完成时间"] = batch.get("完成时间") or now_text()
        batch["更新时间"] = now_text()
        if batch["状态"] == STATUS_COMPLETE:
            self._publish_summary_locked(batch)
        self._sync_report_status_locked(batch)

    def _sync_report_status_locked(self, batch: dict[str, Any]) -> None:
        """报告只同步扫描状态；成本字段只在摘要发布时写入，避免半份结果污染历史结论。"""
        report_number = batch.get("报告编号")
        if not report_number:
            return
        report = self._report_by_number_locked(report_number)
        if report is None:
            return
        report["扫描批次"] = batch["批次编号"]
        report["扫描状态"] = batch["状态"]
        if batch["状态"] == STATUS_PARTIAL:
            report["扫描失败原因"] = "、".join(
                f"{seg['表名称']}/{seg['分区']}" for seg in batch["分段"] if seg["状态"] == STATUS_FAILED
            )
        elif batch["状态"] != STATUS_COMPLETE:
            report["扫描失败原因"] = ""
        report["扫描进度"] = deepcopy(batch["进度"])

    def _publish_summary_locked(self, batch: dict[str, Any]) -> None:
        """只有全部成功的批次才发布摘要。

        发布动作在锁内做一次，且发布的版本号取自分段结果的最大版本：
        重复提交同一采样批次、或新采样没有覆盖到任何分段时都不会重复发布；
        并发请求争抢最后一个分段时，只有真正完成全部扫描的那一次会写入预计节省。
        """
        segments = batch["分段"]
        if not segments or any(seg["状态"] != STATUS_DONE for seg in segments):
            return
        data_version = max(int(seg.get("结果版本") or 0) for seg in segments)
        if data_version <= 0 or batch.get("发布版本") == data_version:
            return
        results = [seg.get("结果") or {} for seg in segments]
        total_bytes = sum(int(item.get("存储字节") or 0) for item in results)
        cold = [
            item
            for item in results
            if int(item.get("最近访问天数") or 0) > COLD_DAYS_THRESHOLD
        ]
        cold_bytes = sum(int(item.get("存储字节") or 0) for item in cold)
        saving_bytes = int(cold_bytes * SAVING_RATIO)
        tables = sorted({str(item.get("表名称")) for item in results})
        generated = now_text()

        summary_id = batch.get("已发布摘要") or ""
        existing_summary_id = ""
        report_number = batch.get("报告编号")
        report = self._report_by_number_locked(report_number) if report_number else None
        if report is not None:
            existing_summary_id = str(report.get("容量摘要编号") or "")
        if existing_summary_id:
            summary_id = existing_summary_id
        elif not summary_id:
            summary_id = f"CAP-{int(batch.get('序号', 1)):04d}"

        summary = {
            "摘要编号": summary_id,
            "扫描范围": batch["分析范围"],
            "存储总量": format_tb(total_bytes),
            "冷数据占比": format_percent(cold_bytes, total_bytes),
            "预计节省": format_tb(saving_bytes),
            "存储总量字节": total_bytes,
            "冷数据字节": cold_bytes,
            "预计节省字节": saving_bytes,
            "覆盖表数": len(tables),
            "覆盖分区数": len(results),
            "扫描分段总数": len(segments),
            "成功分段数": len(results),
            "失败分段数": 0,
            "覆盖状态": "完整覆盖",
            "冷数据阈值": f"最近 {COLD_DAYS_THRESHOLD} 天未访问",
            "采样批次": batch.get("采样批次", 1),
            "生成时间": generated,
            "来源批次": batch["批次编号"],
            "来源报告": report_number or "",
            "更新序号": int((store.capacity_summary(summary_id) or {}).get("更新序号", 0)) + 1,
        }
        existing = store.capacity_summary(summary_id)
        if existing is None:
            store.capacity_summaries().append(summary)
        else:
            existing.clear()
            existing.update(summary)

        if report is not None:
            report["存储总量"] = summary["存储总量"]
            report["冷数据占比"] = summary["冷数据占比"]
            report["预计节省"] = summary["预计节省"]
            report["生成时间"] = generated
            report["容量摘要编号"] = summary_id
            report["扫描批次"] = batch["批次编号"]
            report["扫描状态"] = STATUS_COMPLETE
            report["优化建议"] = (
                f"将 {len(cold)} 个冷分区转归档存储（阈值 {summary['冷数据阈值']}）"
                if cold
                else "未发现超过冷数据阈值的分区，无需转档"
            )
            report["status"] = "已生成"
            report["pending"] = True
            report["abnormal"] = False

        batch["已发布摘要"] = summary_id
        batch["发布版本"] = data_version
        batch["汇总发布次数"] = int(batch.get("汇总发布次数", 0)) + 1
        batch["汇总时间"] = generated

    def _caliber(self, scope: str, sampling_version: int) -> dict[str, Any]:
        return {
            "分析范围": scope,
            "冷数据阈值": f"最近 {COLD_DAYS_THRESHOLD} 天未访问",
            "存储计量": "按分区实际占用字节统计，十进制 TB（1 TB = 1e12 字节）",
            "节省口径": f"冷数据字节 × {int(SAVING_RATIO * 100)}%（转归档存储）",
            "采样批次": sampling_version,
            "分段粒度": "表 + 分区",
        }

    # ---------- 工具 ----------

    def _find_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.scan_batches():
            if row.get("批次编号") == batch_id:
                return row
        return None

    def _find_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        for row in store.scan_batches():
            if row.get("范围指纹") == fingerprint:
                return row
        return None

    def _find_segment(self, batch: dict[str, Any], key: str) -> dict[str, Any] | None:
        for seg in batch["分段"]:
            if seg["分段键"] == key:
                return seg
        return None

    @staticmethod
    def _claim_is_current(segment: dict[str, Any], claim_token: str) -> bool:
        """分段仍由本次认领持有；超时被回收并重新认领后，旧线程不得写回。"""
        return (
            segment.get("状态") == STATUS_RUNNING
            and segment.get("认领标识") == claim_token
        )

    def _report_by_number_locked(self, report_number: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("报告编号") == report_number:
                return row
        return None

    def _reclaim_stale_locked(self, batch: dict[str, Any]) -> int:
        """认领超时的分段（例如执行进程被中断）可以回到待扫描，避免永久卡住。

        返回本次被回收的分段数：回收意味着有请求在别的线程里还没跑完，
        失败原因里会记录中断时间，只有同一批次不会永久停在「扫描中」。
        """
        cutoff = datetime.now(_CST) - timedelta(seconds=STALE_CLAIM_SECONDS)
        reclaimed = 0
        for seg in batch["分段"]:
            if seg["状态"] != STATUS_RUNNING or not seg.get("认领时间"):
                continue
            try:
                claimed = datetime.strptime(seg["认领时间"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST)
            except ValueError:
                claimed = cutoff
            if claimed <= cutoff:
                seg["状态"] = STATUS_PENDING
                seg["认领时间"] = ""
                seg["认领标识"] = ""
                seg["中断次数"] = int(seg.get("中断次数", 0)) + 1
                seg["失败原因"] = {}
                reclaimed += 1
        self._refresh_status_locked(batch)
        return reclaimed


scan_service = CostScanService()
