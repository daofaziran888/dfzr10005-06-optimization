"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.seed import SEED_CAPACITY_SUMMARIES, SEED_ROWS, SEED_SCAN_BATCHES


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 成本扫描的批次与容量摘要单独存放：它们不是 16 个业务模块之一，
        # 不参与概览卡片的模块计数，但和业务表一样随进程常驻，刷新页面不丢进度。
        self._scan_batches: list[dict[str, Any]] = [
            deepcopy(row) for row in SEED_SCAN_BATCHES
        ]
        self._capacity_summaries: list[dict[str, Any]] = [
            deepcopy(row) for row in SEED_CAPACITY_SUMMARIES
        ]

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def scan_batches(self) -> list[dict[str, Any]]:
        return self._scan_batches

    def capacity_summaries(self) -> list[dict[str, Any]]:
        return self._capacity_summaries

    def capacity_summary(self, summary_id: str) -> dict[str, Any] | None:
        for row in self._capacity_summaries:
            if row.get("摘要编号") == summary_id:
                return row
        return None

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
