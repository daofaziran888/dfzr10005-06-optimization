"""存储成本优化业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "cost"
REQUIRED_FIELDS = ["报告编号", "分析范围", "存储总量"]
STATUS_ORDER = ["草稿", "分析中", "已生成", "已执行"]
ACTION_RULES = {"开始分析": "草稿", "执行优化": "已生成", "归档报告": "已执行"}
NEGATIVE_ACTIONS = []


class CostService:
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
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"成本分析报告 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于存储成本优化可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"成本分析报告已{action}"

    def bind_capacity_summary(self, entry_id: int, summary_id: str) -> tuple[dict[str, Any] | None, str]:
        """把已发布的容量摘要挂到报告上，报告侧只读引用，不复制扫描口径。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"成本分析报告 {entry_id} 不存在或已归档"
        summary = store.capacity_summary(summary_id)
        if summary is None:
            return None, f"容量摘要 {summary_id} 不存在，请先完成一次扫描"
        entry["容量摘要编号"] = summary_id
        entry["存储总量"] = summary["存储总量"]
        entry["冷数据占比"] = summary["冷数据占比"]
        entry["预计节省"] = summary["预计节省"]
        entry["生成时间"] = summary["生成时间"]
        entry["扫描状态"] = summary.get("覆盖状态", "完整覆盖")
        return entry, f"成本分析报告已引用容量摘要 {summary_id}"
