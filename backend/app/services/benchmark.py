"""查询性能分析业务规则：状态流转、字段校验与筛选口径都收在这里。

查询性能报告可以引用一次成本扫描产出的容量摘要（容量摘要编号）。引用是只读的：
报告只同步展示本次扫描范围、扫描批次与覆盖状态，不回写摘要，也不重新测算成本。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "benchmark"
REQUIRED_FIELDS = ["报告编号", "查询语句", "执行耗时"]
STATUS_ORDER = ["草稿", "分析中", "已生成", "已优化"]
ACTION_RULES = {"开始分析": "草稿", "应用优化": "已生成", "归档报告": "已优化"}
NEGATIVE_ACTIONS = []


def _latest_summary() -> dict[str, Any] | None:
    summaries = store.capacity_summaries()
    if not summaries:
        return None
    return max(
        summaries,
        key=lambda row: (str(row.get("生成时间", "")), int(row.get("更新序号", 0))),
    )


def with_capacity_scope(entry: dict[str, Any]) -> dict[str, Any]:
    """把报告引用的容量摘要同步成展示字段。

    未显式引用时回落到最近一次已发布的扫描范围，保证查询性能报告
    在成本扫描完成后同步显示本次扫描范围；摘要不存在时给出失效提示。
    """
    summary_id = str(entry.get("容量摘要编号") or "")
    if summary_id:
        summary = store.capacity_summary(summary_id)
        if summary is None:
            entry["扫描范围"] = "未引用容量摘要"
            entry["扫描状态"] = f"引用的容量摘要 {summary_id} 已失效"
            return entry
        entry["引用方式"] = "显式引用"
    else:
        summary = _latest_summary()
        if summary is None:
            entry["扫描范围"] = "未引用容量摘要"
            entry["扫描状态"] = "暂无数据"
            return entry
        entry["容量摘要编号"] = summary.get("摘要编号", "")
        entry["引用方式"] = "同步最新扫描"
    entry["扫描范围"] = summary.get("扫描范围", "")
    entry["扫描批次"] = summary.get("来源批次", "")
    entry["扫描状态"] = summary.get("覆盖状态", "完整覆盖")
    entry["扫描时间"] = summary.get("生成时间", "")
    entry["扫描存储总量"] = summary.get("存储总量", "")
    entry["扫描冷数据占比"] = summary.get("冷数据占比", "")
    entry["扫描预计节省"] = summary.get("预计节省", "")
    return entry


class BenchmarkService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("报告编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [with_capacity_scope(dict(row)) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return with_capacity_scope(dict(row)) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        summary_id = str(values.get("容量摘要编号") or "").strip()
        if summary_id and store.capacity_summary(summary_id) is None:
            return None, [f"容量摘要编号 {summary_id}（摘要不存在或尚未发布）"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        if summary_id:
            entry["容量摘要编号"] = summary_id
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"性能报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询性能分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"性能报告已{action}"
