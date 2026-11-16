"""数据清洗接口：维护清洗规则，覆盖提交校验、启用规则、停用规则与清洗执行。

注意路由声明顺序：/export、/results、/abnormal、/batches、/ledger 等字面量
路径必须放在 /{entry_id} 之前，否则会被路径参数抢占而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cleanse import CleanseService

router = APIRouter(prefix="/api/cleanse", tags=["数据清洗"])

service = CleanseService()

LIST_FIELDS = ["规则编号", "关联数据集", "空值策略", "格式校验", "异常过滤", "去重字段", "规则版本", "生效日期"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤数据清洗列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条清洗规则：规则版本、空值策略、格式校验等契约字段一起落库，缺必填字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="清洗规则已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据清洗清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cleanse", "total": total, "items": items}


@router.get("/results", response_model=PageResult[dict])
def list_results(
    dataset: str | None = Query(default=None, description="按关联数据集过滤"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """清洗后的数据集：按批次执行清洗得到的标准化记录，可按数据集复核。"""
    items, total = service.list_results(dataset=dataset, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/abnormal", response_model=PageResult[dict])
def list_abnormal(
    dataset: str | None = Query(default=None, description="按关联数据集过滤"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """异常过滤记录：每条都带来源批次、规则版本与过滤原因，可回溯。"""
    items, total = service.list_abnormal(dataset=dataset, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    dataset: str | None = Query(default=None, description="按关联数据集过滤"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """批次与断点：已完成批次即恢复点，中断或失败的批次带原因说明。"""
    items, total = service.list_batches(dataset=dataset, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/ledger", response_model=PageResult[dict])
def list_ledger(
    dataset: str | None = Query(default=None, description="按关联数据集过滤"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """清洗字段台账：只存放清洗后同步过来的标准化记录，后续查询只读这里。"""
    items, total = service.list_ledger(dataset=dataset, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清洗规则明细（含执行摘要）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清洗规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条清洗规则执行提交校验、启用规则、停用规则；越级流转会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/execute", response_model=ActionResult)
def execute(entry_id: int, payload: EntryPayload) -> ActionResult:
    """按批次执行清洗：从最后完整批次断点续传，结果同步清洗字段台账。

    values 支持：批次日期（版本仲裁基准）、中断演练/中断演练批次（模拟超时中断）、
    重新执行（清空该数据集既有结果后重跑）。
    """
    report, message, ok = service.execute(entry_id, payload.values)
    return ActionResult(ok=ok, message=message, entry=report)
