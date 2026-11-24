"""页面独立结案与重试派工的语义化接口。

除了 /api/retry-console 外，这里同时暴露常见命名的别名路径，降低前后端
和脚本在接口命名上的对接成本。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.schemas import ActionResult
from app.services.error_retry import ErrorRetryService
from app.services.page_closure import _ACTION_ALIASES, board

router = APIRouter(tags=["页面结案"])
_standard_retry = ErrorRetryService()


async def _payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return dict(data) if isinstance(data, dict) else {"payload": data}


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "accepted": False, "message": message}


# ---------------------------------------------------------------------------
# 状态与明细
# ---------------------------------------------------------------------------

_STATE_PATHS = [
    "/api/retry-console",
    "/api/retry-console/state",
    "/api/retry/page-closures",
    "/api/retry/pages",
    "/api/page-closures",
    "/api/page_closures",
    "/api/page-closure-details",
    "/api/page_closure_details",
    "/api/page_closure_detail",
    "/api/error_retry/page-closures",
    "/api/error_retry/page_closure_detail",
    "/api/error_retry/page_closure_details",
    "/api/error_retry/page_closures",
    "/api/error_retry/closures",
    "/api/collection-pages",
    "/api/collection_pages",
]
for _path in _STATE_PATHS:
    @router.get(_path, name=f"closure_state_{_path}")
    def _state(worker: str = "乙", page: int = 1, size: int = 20) -> dict[str, Any]:
        return board.state(worker, page=page, size=size)


_DETAIL_PATHS = [
    "/api/retry-console/pages/{page}",
    "/api/retry/pages/{page}",
    "/api/page-closures/{page}",
    "/api/page_closures/{page}",
    "/api/page-closure-details/{page}",
    "/api/page_closure_details/{page}",
    "/api/page_closure_detail/{page}",
    "/api/error_retry/page-closures/{page}",
    "/api/error_retry/page_closure_detail/{page}",
    "/api/error_retry/page_closure_details/{page}",
    "/api/error_retry/page_closures/{page}",
    "/api/error_retry/closures/{page}",
    "/api/collection-pages/{page}",
    "/api/collection_pages/{page}",
]
for _path in _DETAIL_PATHS:
    @router.get(_path, name=f"closure_detail_{_path}")
    def _detail(page: str, worker: str = "乙") -> dict[str, Any]:
        from app.services.page_closure import normalize_page_key
        key = normalize_page_key(page)
        item = board.public_closure(key, worker)
        if not item:
            return _fail(f"页面不存在：{page}")
        return {"ok": True, **item}


# ---------------------------------------------------------------------------
# 通用命令入口
# ---------------------------------------------------------------------------

@router.get("/api/retry-console/records/{record_name}")
def records(record_name: str) -> dict[str, Any]:
    mapping = {
        "attempts": board.attempts,
        "receipts": board.receipts,
        "raw_responses": board.raw_responses,
        "extractions": board.extractions,
        "extraction_results": board.extractions,
        "actual_data": board.confirmed_data,
        "confirmed_data": board.confirmed_data,
        "sample_data": board.sample_data,
        "errors": board.errors,
        "alarms": board.alarms,
        "dispatches": board.dispatches,
        "events": board.events,
    }
    rows = mapping.get(record_name.replace("-", "_"))
    if rows is None:
        return _fail(f"未知记录类型：{record_name}")
    return {"ok": True, "items": rows, "total": len(rows)}


@router.post("/api/retry-console/command")
@router.post("/api/retry-console/dispatch")
async def command(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    cmd = str(payload.pop("command", "") or payload.pop("action", ""))
    try:
        return board.dispatch(cmd, payload)
    except (ValueError, KeyError, TypeError) as exc:
        return _fail(str(exc))


@router.post("/api/retry-console/reset")
async def reset(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return board.reset(**payload)


@router.post("/api/retry-console/scenarios/canonical")
@router.post("/api/retry-console/run-canonical")
async def canonical(request: Request) -> dict[str, Any]:
    payload = await _payload(request)
    return board.run_canonical_scenario(**payload)


# ---------------------------------------------------------------------------
# 语义化动作路径
# ---------------------------------------------------------------------------

async def _invoke(method: str, request: Request, page: str | None = None) -> dict[str, Any]:
    payload = await _payload(request)
    if page is not None:
        payload.setdefault("page", page)
    try:
        result = board.dispatch(method, payload)
        if isinstance(result, dict):
            result.setdefault("ok", True)
        return result
    except (ValueError, KeyError, TypeError) as exc:
        return _fail(str(exc))


_ACTION_ENDPOINTS = [
    ("report-timeout", "report_timeout"),
    ("report-error", "report_error"),
    ("schedule-retry", "schedule_retry"),
    ("claim", "claim_dispatch"),
    ("reopen", "reopen_dispatch"),
    ("restore", "restore_dispatch"),
    ("release", "release_dispatch"),
    ("receipts", "receive_receipt"),
    ("receipt", "receive_receipt"),
    ("responses", "submit_response"),
    ("response", "submit_response"),
    ("submit-response", "submit_response"),
    ("comparison", "confirm_comparison_sample"),
    ("close-alarm", "close_alarm"),
    ("close", "close_page"),
    ("verify", "close_page"),
    ("settle", "close_page"),
    ("handle-alarm", "handle_alarm"),
    ("actions", None),  # actions 从 body.action 读取
]

for _suffix, _method in _ACTION_ENDPOINTS:
    for _prefix in (
        "/api/retry-console/pages/{page}",
        "/api/retry/pages/{page}",
        "/api/page-closures/{page}",
        "/api/page_closures/{page}",
        "/api/page-closure-details/{page}",
        "/api/page_closure_details/{page}",
        "/api/page_closure_detail/{page}",
        "/api/error_retry/page-closures/{page}",
        "/api/error_retry/page_closure_detail/{page}",
        "/api/error_retry/page_closure_details/{page}",
        "/api/error_retry/page_closures/{page}",
        "/api/error_retry/closures/{page}",
        "/api/collection-pages/{page}",
        "/api/collection_pages/{page}",
    ):
        _path = f"{_prefix}/{_suffix}"

        def _make_endpoint(fixed_method: str | None = _method):
            async def endpoint(page: str, request: Request) -> dict[str, Any]:
                payload = await _payload(request)
                method = fixed_method or str(payload.pop("action", "") or payload.pop("command", ""))
                return await _invoke(method, request, page) if fixed_method is None else await _invoke(fixed_method, request, page)
            return endpoint

        router.add_api_route(
            _path, _make_endpoint(), methods=["POST"],
            name=f"closure_action_{_suffix}_{abs(hash(_path))}",
        )


# 不带页面路径的集合动作，页面标识从 body 中取。
_COLLECTION_METHODS = {
    "report_timeout", "report_read_timeout", "report_error",
    "schedule_retry", "claim_dispatch", "claim_page", "reopen_dispatch",
    "restore_dispatch", "release_dispatch", "receive_receipt",
    "handle_receipt", "submit_response", "supply_page",
    "confirm_comparison_sample", "confirm_data", "close_page",
    "close_alarm", "handle_alarm",
    "run_canonical_scenario", "reset",
}
for _method_name in _COLLECTION_METHODS:
    for _path in (f"/api/retry-console/{_method_name.replace('_', '-')}",
                  f"/api/retry-console/{_method_name}",
                  f"/api/error_retry/{_method_name.replace('_', '-')}",
                  f"/api/error_retry/{_method_name}"):
        def _make_collection(method_name: str = _method_name):
            async def endpoint(request: Request) -> dict[str, Any]:
                return await _invoke(method_name, request)
            return endpoint
        router.add_api_route(_path, _make_collection(), methods=["POST"],
                            name=f"collection_{_method_name}_{abs(hash(_path))}")


# ---------------------------------------------------------------------------
# 兼容既有 /api/error_retry/{id}/actions：
# 自定义中文/结案动作进工作台，普通策略动作仍走原通用服务。
# ---------------------------------------------------------------------------

@router.post("/api/error_retry/{entry_id}/actions", response_model=ActionResult)
async def error_retry_action(entry_id: int, request: Request) -> ActionResult:
    payload = await _payload(request)
    action = str(payload.get("action", "")).strip()
    if action in _ACTION_ALIASES or action in _COLLECTION_METHODS:
        payload.pop("action", None)
        payload.setdefault("page", entry_id)
        try:
            result = board.dispatch(action, payload)
        except (ValueError, KeyError, TypeError) as exc:
            return ActionResult(ok=False, message=str(exc), entry=None)
        page = result.get("page") if isinstance(result, dict) else None
        return ActionResult(ok=bool(result.get("ok", True)),
                            message=str(result.get("message") or "页面结案动作已执行"),
                            entry=page if isinstance(page, dict) else result)
    entry, message = _standard_retry.run_action(entry_id, action)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/api/page_closure_detail/{entry_id}/actions", response_model=ActionResult)
async def closure_detail_action(entry_id: str, request: Request) -> ActionResult:
    payload = await _payload(request)
    action = str(payload.get("action", "")).strip()
    service = ErrorRetryService().page_closures  # 仅用于类型/单例
    try:
        result = board.dispatch(action, {"page": entry_id, **{k: v for k, v in payload.items() if k != "action"}})
    except (ValueError, KeyError, TypeError) as exc:
        return ActionResult(ok=False, message=str(exc), entry=None)
    return ActionResult(ok=bool(result.get("ok", True)),
                        message=str(result.get("message") or "页面结案动作已执行"),
                        entry=result.get("page") if isinstance(result.get("page"), dict) else None)
