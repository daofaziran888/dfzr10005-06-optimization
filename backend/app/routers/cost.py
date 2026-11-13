"""存储成本优化接口：报告维护 + 按表/分区分段扫描。

扫描相关接口放在 /api/cost/scans 下，与报告 CRUD 分开：
- POST   /api/cost/scans                     提交范围，相同范围只保留一个批次
- GET    /api/cost/scans                     批次列表（含进度与失败数）
- GET    /api/cost/scans/{batch_id}          批次明细（分段状态、失败原因、成本口径）
- POST   /api/cost/scans/{batch_id}/resume   从断点继续扫描
- POST   /api/cost/scans/{batch_id}/retry    只重试失败段
- GET    /api/cost/scans/{batch_id}/segments 只看分段，便于前端轮询
- GET    /api/cost/capacity-summaries       容量摘要列表（性能分析引用）
- GET    /api/cost/capacity-summaries/{id}  容量摘要明细
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cost import CostService
from app.services.cost_scan import scan_service

router = APIRouter(prefix="/api/cost", tags=["存储成本优化"])

service = CostService()

LIST_FIELDS = ["报告编号", "分析范围", "存储总量", "冷数据占比", "优化建议", "预计节省", "执行状态", "生成时间"]
STATUSES = ["草稿", "分析中", "已生成", "已执行"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    status: str | None = Query(default=None, description="草稿、分析中、已生成、已执行"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按报告编号与状态过滤存储成本优化列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条成本分析报告，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="成本分析报告已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出存储成本优化清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cost", "total": total, "items": items}


# ---------- 分段扫描 ----------


@router.post("/scans", response_model=ActionResult)
def create_scan(payload: EntryPayload) -> ActionResult:
    """提交扫描范围。相同范围指纹只保留一个批次，重复提交复用已有进度。"""
    values = payload.values
    scope = str(values.get("分析范围") or values.get("scope") or "").strip()
    report_id = values.get("report_id")
    if report_id is not None:
        try:
            report_id = int(report_id)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message="report_id 必须是成本分析报告的数字编号")
    try:
        sampling_version = int(values.get("采样批次") or values.get("sampling_version") or 1)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="采样批次必须是大于等于 1 的整数")
    try:
        max_segments = int(values["max_segments"]) if values.get("max_segments") is not None else None
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    if max_segments is not None and max_segments < 1:
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    batch, message = scan_service.create_scan(
        scope=scope,
        sampling_version=sampling_version,
        report_id=report_id,
        max_segments=max_segments,
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/scans", response_model=PageResult[dict])
def list_scans(
    keyword: str | None = Query(default=None, description="按批次编号或分析范围检索"),
    status: str | None = Query(default=None, description="待扫描、扫描中、已中断、部分失败、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出扫描批次及进度；没有批次时返回空页，前端展示空态。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = scan_service.list_scans(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/scans/{batch_id}", response_model=dict)
def get_scan(batch_id: str) -> dict:
    """读取批次明细：分段状态、失败原因、当前成本口径与已发布摘要。"""
    batch = scan_service.get_scan(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"扫描批次 {batch_id} 不存在")
    return batch


@router.get("/scans/{batch_id}/segments", response_model=dict)
def get_scan_segments(batch_id: str) -> dict:
    """只返回进度与分段列表，供前端刷新后恢复同一进度。"""
    batch = scan_service.get_scan(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"扫描批次 {batch_id} 不存在")
    return {
        "批次编号": batch["批次编号"],
        "状态": batch["状态"],
        "进度": batch["进度"],
        "中断原因": batch.get("中断原因", ""),
        "分段": batch["分段"],
    }


@router.post("/scans/{batch_id}/resume", response_model=ActionResult)
def resume_scan(batch_id: str, payload: EntryPayload) -> ActionResult:
    """从断点继续扫描：只处理待扫描分段，已完成分段保持不变。"""
    try:
        max_segments = int(payload.values["max_segments"]) if payload.values.get("max_segments") is not None else None
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    if max_segments is not None and max_segments < 1:
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    batch, message = scan_service.resume_scan(batch_id, max_segments=max_segments)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/scans/{batch_id}/retry", response_model=ActionResult)
def retry_failed(batch_id: str, payload: EntryPayload) -> ActionResult:
    """只重试失败段；不可重试的失败会原样保留并给出人工处理建议。"""
    try:
        max_segments = int(payload.values["max_segments"]) if payload.values.get("max_segments") is not None else None
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    if max_segments is not None and max_segments < 1:
        return ActionResult(ok=False, message="max_segments 必须是正整数或留空")
    batch, message = scan_service.retry_failed(batch_id, max_segments=max_segments)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


# ---------- 容量摘要 ----------


@router.get("/capacity-summaries", response_model=PageResult[dict])
def list_capacity_summaries(
    keyword: str | None = Query(default=None, description="按摘要编号或扫描范围检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出供查询性能分析引用的容量摘要；没有数据时返回空页。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = scan_service.list_capacity_summaries(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/capacity-summaries/{summary_id}", response_model=dict)
def get_capacity_summary(summary_id: str) -> dict:
    """读取容量摘要明细；被引用的摘要不会因重扫而更换编号。"""
    summary = scan_service.get_capacity_summary(summary_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"容量摘要 {summary_id} 不存在或尚未发布")
    return summary


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条成本分析报告明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"成本分析报告 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条成本分析报告执行开始分析、执行优化、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/capacity-summary", response_model=ActionResult)
def bind_capacity_summary(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把已有容量摘要绑定到报告，保留原始成本字段供历史结论核对。"""
    summary_id = str(payload.values.get("容量摘要编号") or "").strip()
    if not summary_id:
        return ActionResult(ok=False, message="缺少必填字段：容量摘要编号")
    entry, message = service.bind_capacity_summary(entry_id, summary_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
