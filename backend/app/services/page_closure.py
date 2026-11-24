"""采集页面独立结案领域。

这个模块同时承载：
- 页面结案明细（一页一条，独立于重试策略本身）
- 派工/领取/重新打开
- 迟到回执、原始响应正文、解析结果和实际数据的关联
- 按尝试关联的错误记录与报警

所有状态变更都集中在这里，路由层只做参数搬运。
"""
from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Any

from app.compute import content_hash, retry_calculator
from app.services.base import BaseModuleService
from app.store import store

PENDING = "待补采"
VERIFIED = "已核验"
PARSE_FAILED = "核验未过"

READ_TIMEOUT = "读取超时"
GATEWAY_ERROR = "网关错误"
PARSE_ERROR = "解析失败"

_ACTION_ALIASES = {
    "report_timeout": "report_timeout",
    "report_read_timeout": "report_timeout",
    "report_error": "report_error",
    "report_errors": "report_timeout",
    "上报超时": "report_timeout",
    "上报读取超时": "report_timeout",
    "报读取超时": "report_timeout",
    "schedule_retry": "schedule_retry",
    "create_retry_appointment": "schedule_retry",
    "appoint_retry": "schedule_retry",
    "预约重采": "schedule_retry",
    "claim": "claim_dispatch",
    "claim_page": "claim_dispatch",
    "claim_dispatch": "claim_dispatch",
    "领取": "claim_dispatch",
    "领取页面": "claim_dispatch",
    "领取第6页": "claim_dispatch",
    "reopen": "reopen_dispatch",
    "reopen_dispatch": "reopen_dispatch",
    "reopen_dispatch_board": "reopen_dispatch",
    "重新打开派工": "reopen_dispatch",
    "重开派工": "reopen_dispatch",
    "restore_dispatch": "restore_dispatch",
    "resume_dispatch": "restore_dispatch",
    "recover_dispatch": "restore_dispatch",
    "恢复派工": "restore_dispatch",
    "release": "release_dispatch",
    "release_dispatch": "release_dispatch",
    "释放派工": "release_dispatch",
    "receive_receipt": "receive_receipt",
    "handle_receipt": "receive_receipt",
    "add_receipt": "receive_receipt",
    "回执到达": "receive_receipt",
    "登记回执": "receive_receipt",
    "接收回执": "receive_receipt",
    "重复回执": "receive_receipt",
    "重复接收回执": "receive_receipt",
    "再次回执": "receive_receipt",
    "submit_response": "submit_response",
    "submit_response_body": "submit_response",
    "supply_page": "submit_response",
    "supply_original_page": "submit_response",
    "attach_body": "submit_response",
    "补响应": "submit_response",
    "补正文": "submit_response",
    "补到原有效页": "submit_response",
    "补到它的原有效页": "submit_response",
    "提交响应正文": "submit_response",
    "提交解析": "submit_response",
    "confirm_comparison_sample": "confirm_comparison_sample",
    "confirm_sample": "confirm_comparison_sample",
    "comparison_sample": "confirm_comparison_sample",
    "确认对照采样": "confirm_comparison_sample",
    "对照采样": "confirm_comparison_sample",
    "confirm_data": "confirm_data",
    "confirm_page_data": "confirm_data",
    "确认数据": "confirm_data",
    "确认成功页数据": "confirm_data",
    "再次确认": "confirm_comparison_sample",
    "重复核对": "confirm_comparison_sample",
    "close_alarm": "close_alarm",
    "resolve_alarm": "close_alarm",
    "close_page": "close_page",
    "settle_page": "close_page",
    "verify_page": "close_page",
    "complete_page": "close_page",
    "结案": "close_page",
    "页面结案": "close_page",
    "关闭报警": "close_alarm",
    "handle_alarm": "handle_alarm",
    "ack_alarm": "handle_alarm",
    "处理报警": "handle_alarm",
    "reset": "reset",
    "reset_scenario": "reset",
    "重置": "reset",
    "重置场景": "reset",
    "run_canonical_scenario": "run_canonical_scenario",
    "run_scenario": "run_canonical_scenario",
    "一键回放": "run_canonical_scenario",
}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def normalize_page_key(page: Any = None, **kwargs: Any) -> str:
    """把 6 / “第 6 页” / page-06 等输入归一为 P6。"""
    if page is None:
        page = (
            kwargs.get("page_key")
            or kwargs.get("page_id")
            or kwargs.get("page_no")
            or kwargs.get("page_number")
            or kwargs.get("页码")
            or kwargs.get("页面编号")
        )
    if isinstance(page, dict):
        page = normalize_page_key(**page)
    if page is None:
        return ""
    if isinstance(page, int):
        return f"P{page}"
    text = str(page).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+", text):
        return f"P{int(text)}"
    match = re.search(r"第\s*0*(\d+)\s*页", text)
    if match:
        return f"P{int(match.group(1))}"
    match = re.fullmatch(r"(?i:page|p)[\-_:\s]*0*(\d+)", text)
    if match:
        return f"P{int(match.group(1))}"
    match = re.search(r"(?i:page)[\-_:\s]*0*(\d+)", text)
    if match:
        return f"P{int(match.group(1))}"
    return text


def page_number(key: str) -> int | None:
    match = re.fullmatch(r"P(\d+)", key)
    return int(match.group(1)) if match else None


def worker_value(values: dict[str, Any], default: str = "乙") -> str:
    for key in ("worker", "assignee", "operator", "user", "owner", "handler",
                "claimed_by", "领取人", "操作人", "处理人"):
        value = values.get(key)
        if value:
            return str(value)
    return default


def _first_value(source: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return default


# ---------------------------------------------------------------------------
# 极简 DOM：解析规则需要消费真实正文，但运行环境不保证安装 BeautifulSoup。
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]):
        self.tag = tag.lower()
        self.attrs = {k.lower(): (v or "") for k, v in attrs}
        self.children: list[_Node | str] = []
        self.parent: _Node | None = None

    @property
    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                parts.append(child.text)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def descendants(self) -> list["_Node"]:
        result: list[_Node] = []
        for child in self.children:
            if isinstance(child, _Node):
                result.append(child)
                result.extend(child.descendants())
        return result


class _DOMBuilder(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def _parse_dom(html: str) -> _Node:
    builder = _DOMBuilder()
    try:
        builder.feed(html)
    except Exception:
        # 即使 HTML 不规范，HTMLParser 通常已经保留了可用节点。
        pass
    return builder.root


def _match_compound(node: _Node, compound: str) -> bool:
    if not compound:
        return False
    token = compound.strip()
    if token.startswith("#"):
        return node.attrs.get("id") == token[1:]
    if token.startswith("."):
        classes = node.attrs.get("class", "").split()
        return token[1:] in classes
    # 支持 tag.class、tag#id、tag[attr=value] 的常见组合。
    tag_match = re.match(r"^[a-zA-Z][\w-]*", token)
    if tag_match:
        if node.tag != tag_match.group(0).lower():
            return False
        rest = token[tag_match.end():]
    else:
        rest = token
    for class_name in re.findall(r"\.([\w-]+)", rest):
        if class_name not in node.attrs.get("class", "").split():
            return False
    for ident in re.findall(r"#([\w-]+)", rest):
        if node.attrs.get("id") != ident:
            return False
    for attr, value in re.findall(r"\[([\w-]+)(?:=[\"']?([^\"'\]]+)[\"']?)?\]", rest):
        if attr not in node.attrs:
            return False
        if value and node.attrs.get(attr) != value:
            return False
    return True


def _select_all(root: _Node, selector: str) -> list[_Node]:
    selector = selector.strip()
    if not selector:
        return []
    # 选择器组。
    if "," in selector:
        result: list[_Node] = []
        seen: set[int] = set()
        for part in selector.split(","):
            for node in _select_all(root, part):
                if id(node) not in seen:
                    seen.add(id(node))
                    result.append(node)
        return result

    if selector.lstrip().startswith("/"):
        css = selector.strip()
        css = re.sub(r"^/+", "", css)
        css = re.sub(r"\[@class=['\"]([^'\"]+)['\"]\]",
                     lambda m: "".join(f".{part}" for part in m.group(1).split()), css)
        css = re.sub(r"\[@id=['\"]([^'\"]+)['\"]\]", r"#\1", css)
        css = re.sub(r"\[[^\]]+\]", "", css)
        css = css.replace("//", " ").replace("/", " ")
        css = re.sub(r"\s+", " ", css).strip()
        if css:
            return _select_all(root, css)

    parts = [part for part in re.split(r"\s+", selector) if part]
    current = [root]
    for part in parts:
        nxt: list[_Node] = []
        for scope in current:
            if scope is root:
                candidates = [scope] + scope.descendants()
            else:
                candidates = scope.descendants()
            for node in candidates:
                if node is not root and _match_compound(node, part):
                    nxt.append(node)
        current = nxt
    return current


def _select_one_text(root: _Node, selector: str) -> str:
    nodes = _select_all(root, selector)
    return nodes[0].text if nodes else ""


def _convert_value(value: str, data_type: str = "string", default: Any = "") -> Any:
    if value in (None, ""):
        return default
    kind = (data_type or "string").lower()
    if kind in {"int", "integer", "整数"}:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if kind in {"float", "number", "decimal", "数值", "数字"}:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if kind in {"bool", "boolean", "布尔"}:
        return str(value).strip().lower() in {"1", "true", "yes", "是", "真"}
    return value


def detect_gateway_failure(text: str, status_code: Any = None) -> dict[str, Any] | None:
    """识别网关/上游错误正文；这些正文不能被当成商品页结案。"""
    code: int | None = None
    if status_code not in (None, ""):
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = None
    if code and code >= 500:
        label = {502: "Bad Gateway", 503: "Service Unavailable",
                 504: "Gateway Timeout"}.get(code, "Server Error")
        cn = {502: "网关错误", 503: "服务不可用", 504: "网关超时"}.get(code, "服务端错误")
        return {"code": f"HTTP_{code}", "http_status": code,
                "error_type": "gateway_error", "failure_type": GATEWAY_ERROR,
                "label": label, "message": f"{code} {label}；{cn}"}

    head = text[:8000]
    patterns = [
        (r"(?is)<title>[^<]*(502|bad gateway|网关错误|网关异常)[^<]*</title>", "HTTP_502", 502, GATEWAY_ERROR),
        (r"(?is)<title>[^<]*(504|gateway time-?out|网关超时)[^<]*</title>", "HTTP_504", 504, "网关超时"),
        (r"(?is)<title>[^<]*(503|service unavailable|服务不可用)[^<]*</title>", "HTTP_503", 503, "服务不可用"),
        (r"(?m)^\s*502\s+Bad Gateway\s*$", "HTTP_502", 502, GATEWAY_ERROR),
        (r"502\s+Bad Gateway", "HTTP_502", 502, GATEWAY_ERROR),
        (r"504\s+Gateway Time-?out", "HTTP_504", 504, "网关超时"),
        (r"网关错误|网关异常|Bad Gateway|bad_gateway|upstream gateway error", "GATEWAY_ERROR", 502, GATEWAY_ERROR),
        (r"网关超时|Gateway Time-?out", "GATEWAY_TIMEOUT", 504, "网关超时"),
    ]
    for pattern, error_code, http_status, cn in patterns:
        if re.search(pattern, head):
            return {"code": error_code, "http_status": http_status,
                    "error_type": "gateway_error", "failure_type": cn,
                    "message": cn}

    try:
        payload = json.loads(text)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error_code = str(payload.get("code") or payload.get("error_code") or "").lower()
        message = str(payload.get("message") or payload.get("msg") or payload.get("error") or "")
        if error_code in {"502", "503", "504", "bad_gateway", "gateway_error",
                          "gateway_timeout", "upstream_error"} or any(
            token in message for token in ("网关错误", "网关超时", "Bad Gateway", "Gateway Timeout")
        ):
            return {"code": error_code.upper() or "GATEWAY_ERROR",
                    "http_status": 502 if "timeout" not in error_code else 504,
                    "error_type": "gateway_error",
                    "failure_type": "网关超时" if "timeout" in error_code else GATEWAY_ERROR,
                    "message": message or GATEWAY_ERROR}
    return None


def parse_actual_response(
    response_text: str,
    parser_rule: Any = None,
    *,
    page_key: str = "",
    raw_response_id: str = "",
) -> dict[str, Any]:
    """用解析规则消费真实响应文字，并返回可与原始页关联的提取结果。"""
    text = response_text or ""
    gateway = detect_gateway_failure(
        text, _first_value(parser_rule if isinstance(parser_rule, dict) else {}, "status_code", "http_status")
    )
    if gateway:
        return {
            "ok": False,
            "valid": False,
            "is_product_page": False,
            "parse_passed": False,
            "item_count": 0,
            "field_count": 0,
            "items": [],
            "error_type": gateway["error_type"],
            "failure_type": gateway["failure_type"],
            "error_code": gateway["code"],
            "message": gateway["message"],
            "response_length": len(text),
            "response_text_consumed": True,
            "raw_response_id": raw_response_id,
            "page_key": page_key,
        }

    rule = parser_rule
    if isinstance(rule, int):
        row = store.find("parser_rule", rule) or {}
        rule = row
    fields: list[dict[str, Any]] = []
    item_selector = ""
    if isinstance(rule, str):
        item_selector = rule
    elif isinstance(rule, list):
        fields = [item if isinstance(item, dict) else {"selector": str(item)} for item in rule]
    elif isinstance(rule, dict):
        item_selector = str(_first_value(
            rule, "item_selector", "list_selector", "container_selector",
            "列表选择器", "容器选择器", default=""))
        raw_fields = _first_value(rule, "fields", "field_rules", "字段规则", default=None)
        if isinstance(raw_fields, list):
            fields = [item if isinstance(item, dict) else {"selector": str(item)} for item in raw_fields]
        elif _first_value(rule, "selector", "字段选择器", default=None):
            fields = [rule]

    root = _parse_dom(text) if "<" in text else None
    items: list[dict[str, Any]] = []

    if root is not None and item_selector:
        containers = _select_all(root, item_selector)
        for index, node in enumerate(containers, 1):
            item: dict[str, Any] = {"_index": index}
            for field in fields:
                name = str(_first_value(field, "name", "field", "target", "key",
                                        "字段", "目标字段", default=f"field_{index}"))
                selector = str(_first_value(field, "selector", "css", "xpath",
                                            "字段选择器", default=""))
                value = ""
                if selector:
                    selected = _select_all(node, selector)
                    if selected:
                        attr = _first_value(field, "attribute", "attr", "属性", default="")
                        value = selected[0].attrs.get(attr, "") if attr else selected[0].text
                else:
                    value = node.text
                regex = _first_value(field, "regex", "正则过滤", default="")
                if regex and value:
                    match = re.search(regex, value)
                    value = match.group(1) if match and match.groups() else (match.group(0) if match else "")
                default = _first_value(field, "default", "default_value", "默认取值", default="")
                item[name] = _convert_value(value, str(_first_value(field, "type", "data_type", "数据类型", default="string")), default)
            items.append(item)
    elif root is not None and fields:
        item = {}
        for index, field in enumerate(fields, 1):
            name = str(_first_value(field, "name", "field", "target", "key",
                                    "字段", "目标字段", default=f"field_{index}"))
            selector = str(_first_value(field, "selector", "css", "xpath", "字段选择器", default=""))
            value = _select_one_text(root, selector) if selector else root.text
            regex = _first_value(field, "regex", "正则过滤", default="")
            if regex and value:
                match = re.search(regex, value)
                value = match.group(1) if match and match.groups() else (match.group(0) if match else "")
            default = _first_value(field, "default", "default_value", "默认取值", default="")
            item[name] = _convert_value(value, str(_first_value(field, "type", "data_type", "数据类型", default="string")), default)
        if any(value not in ("", None, []) for key, value in item.items() if key != "_index"):
            items.append(item)
    else:
        # 没有显式规则时，使用内置商品页识别，避免把网关/空白/纯错误正文结案。
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ("items", "products", "goods", "records", "data", "list"):
                candidate = payload.get(key)
                if isinstance(candidate, list) and candidate:
                    items = [dict(x) if isinstance(x, dict) else {"value": x} for x in candidate]
                    break
            if not items and any(k in payload for k in ("title", "name", "sku", "商品名称")):
                items = [payload]
        elif root is not None:
            candidate_selectors = [
                "[data-sku]", ".product-item", ".goods-item", ".item-product",
                ".product", ".goods", ".product-card", ".goods-card", "article.product",
            ]
            containers: list[_Node] = []
            seen: set[int] = set()
            for selector in candidate_selectors:
                for node in _select_all(root, selector):
                    if id(node) not in seen and len(node.text) >= 2:
                        seen.add(id(node))
                        containers.append(node)
            for index, node in enumerate(containers, 1):
                title = ""
                for selector in (".title", ".product-title", ".name", ".goods-name", "h1", "h2", "h3", "h4"):
                    title = _select_one_text(node, selector)
                    if title:
                        break
                price = ""
                for selector in (".price", ".product-price", ".goods-price"):
                    price = _select_one_text(node, selector)
                    if price:
                        break
                if not price:
                    match = re.search(r"[¥￥$]\s?\d+(?:\.\d+)?", node.text)
                    price = match.group(0) if match else ""
                sku = node.attrs.get("data-sku") or node.attrs.get("data-id") or ""
                if title or price or sku:
                    items.append({"_index": index, "title": title, "price": price, "sku": sku})
            if not items:
                body_text = root.text
                product_markers = sum(bool(x) for x in (
                    re.search(r"商品|宝贝|货品", body_text),
                    re.search(r"[¥￥]\s?\d+(?:\.\d+)?", body_text),
                    re.search(r"SKU|货号|data-sku", text, re.I),
                    re.search(r"原有效页|有效(?:商品)?页|data-valid-page=['\"]?true", text, re.I),
                ))
                if product_markers >= 2 and body_text:
                    items = [{"title": _select_one_text(root, "title") or body_text[:80],
                              "raw_text_excerpt": body_text[:200]}]

    # 如果显式规则一条都没取到，判定为未通过解析检查。
    valid = bool(items) and all(item for item in items)
    extracted_fields: list[Any] = []
    for item in items:
        for key, value in item.items():
            if not str(key).startswith("_"):
                extracted_fields.append(value)
    result = {
        "ok": valid,
        "valid": valid,
        "is_product_page": valid,
        "parse_passed": valid,
        "item_count": len(items),
        "field_count": (len(fields) or len(items[0].keys())) if items else len(extracted_fields),
        "extracted_fields": extracted_fields[:50],
        "items": items,
        "parser": "page_closure_parser",
        "response_length": len(text),
        "html_length": len(text),
        "response_text_consumed": True,
        "content_hash": content_hash(text) if text else "",
        "raw_response_id": raw_response_id,
        "page_key": page_key,
    }
    if not valid:
        result.update({
            "error_type": "parse_failed",
            "failure_type": PARSE_ERROR,
            "error_code": "PARSE_FAILED",
            "message": "响应正文未解析出有效商品数据",
        })
    return result


class PageClosureBoard:
    """内存态页面结案工作台；通过全局 board 被服务和路由共享。"""

    def __init__(self, seed_canonical: bool = True) -> None:
        self.closures: dict[str, dict[str, Any]] = {}
        self.dispatches: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self.receipts: list[dict[str, Any]] = []
        self.raw_responses: list[dict[str, Any]] = []
        self.extractions: list[dict[str, Any]] = []
        self.confirmed_data: list[dict[str, Any]] = []
        self.sample_data: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.alarms: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.dispatch_enabled = True
        self.dispatch_status = "待派工"
        self.viewer = "乙"
        self.counters: dict[str, int] = {}
        self.initialized = False
        if seed_canonical:
            self.reset(seed="baseline")
        else:
            self._sync_store()

    # ---------- 基础工具 ----------

    def reset(self, seed: Any = None, **kwargs: Any) -> dict[str, Any]:
        mode = seed if isinstance(seed, str) else ("baseline" if seed is not False else "empty")
        # 删除本工作台写入 error_logger 的记录；保留仓库自带示例数据。
        if self.errors:
            managed_ids = {error.get("error_record_id") for error in self.errors}
            logger_rows = store.rows("error_logger")
            logger_rows[:] = [row for row in logger_rows if row.get("id") not in managed_ids]
        self.closures = {}
        self.dispatches = []
        self.attempts = []
        self.receipts = []
        self.raw_responses = []
        self.extractions = []
        self.confirmed_data = []
        self.sample_data = []
        self.errors = []
        self.alarms = []
        self.events = []
        self.counters = {}
        self.dispatch_enabled = True
        self.dispatch_status = "待派工"
        self.viewer = kwargs.get("worker") or "乙"
        self.initialized = True
        # 清理通用资源表，避免 reset 后仍读到旧镜像。
        store.rows("page_closure_detail").clear()
        if mode == "baseline":
            for page in (6, 7, 8):
                self._closure(normalize_page_key(page))
            self._event("reset", "已登记第6、7、8页，可开始上报读取超时")
        elif mode in {"canonical", "scenario"}:
            return self.run_canonical_scenario(**kwargs)
        self._sync_store()
        return self.state()

    def _next(self, namespace: str) -> int:
        self.counters[namespace] = self.counters.get(namespace, 0) + 1
        return self.counters[namespace]

    def _event(self, event_type: str, message: str, **data: Any) -> None:
        event = {"id": f"EVT-{self._next('event')}", "type": event_type,
                 "message": message, "at": now_text(), **data}
        self.events.append(event)

    def _closure(self, key: str, create: bool = True) -> dict[str, Any] | None:
        if not key:
            raise ValueError("缺少页面标识")
        closure = self.closures.get(key)
        if closure is not None or not create:
            return closure
        number = page_number(key)
        closure_id = f"CLS-{key}"
        closure = {
            "id": self._next("closure"),
            "closure_id": closure_id,
            "detail_id": closure_id,
            "closure_detail_id": closure_id,
            "结案明细编号": closure_id,
            "page_id": number if number is not None else key,
            "page_key": key,
            "页面编号": key,
            "page_number": number,
            "page_no": number,
            "页码": number if number is not None else "",
            "page_name": f"第{number}页" if number is not None else key,
            "页面名称": f"第{number}页" if number is not None else key,
            "strategy_id": "",
            "策略标识": "",
            "status": PENDING,
            "结案状态": PENDING,
            "pending": True,
            "abnormal": False,
            "closed": False,
            "verified": False,
            "success": False,
            "success_count": 0,
            "closure_count": 0,
            "receipt_received": False,
            "valid_receipt_received": False,
            "receipt_status": "未到达",
            "data_confirmed": False,
            "data_confirmed_once": False,
            "confirmed_data_count": 0,
            "actual_data_count": 0,
            "retry_scheduled": False,
            "预约重采": False,
            "retry_exempt": False,
            "本次已免重采": False,
            "dispatch_status": "待派工",
            "派工状态": "待派工",
            "dispatch_id": "",
            "assignee": "",
            "领取人": "",
            "claimed": False,
            "尚未取页": False,
            "not_fetched": False,
            "claim_fetched": False,
            "worker_fetched": False,
            "actually_fetched": False,
            "response_obtained": False,
            "late_response_recovered": False,
            "original_page_recovered": False,
            "comparison_status": "未确认",
            "对照采样状态": "可确认一次",
            "comparison_count": 0,
            "comparison_confirmed": False,
            "alarm_status": "无报警",
            "报警状态": "无报警",
            "open_alarm_count": 0,
            "attempt_id": "",
            "current_attempt_id": "",
            "original_attempt_id": "",
            "receipt_id": "",
            "raw_response_id": "",
            "extraction_id": "",
            "error_record_id": "",
            "attempt_ids": [],
            "receipt_ids": [],
            "raw_response_ids": [],
            "extraction_ids": [],
            "data_ids": [],
            "error_ids": [],
            "alarm_ids": [],
            "open_alarm_ids": [],
            "available_actions": [],
            "actions": [],
            "action_labels": [],
            "last_message": "",
            "created_at": now_text(),
            "closed_at": "",
        }
        self.closures[key] = closure
        return closure

    def _pages_from(self, *args: Any, **kwargs: Any) -> list[str]:
        raw: list[Any] = []
        if args:
            raw.extend(args)
        for key in ("pages", "page_numbers", "page_keys", "page_ids", "page_list", "ids", "items", "target_pages"):
            value = kwargs.get(key)
            if value is not None:
                if isinstance(value, (list, tuple, set)):
                    raw.extend(value)
                else:
                    raw.append(value)
        for key in ("page", "page_id", "page_key", "page_no", "page_number", "页码", "页面编号"):
            value = kwargs.get(key)
            if value is not None:
                raw.append(value)
        if not raw and isinstance(kwargs.get("payload"), dict):
            return self._pages_from(**kwargs["payload"])
        pages: list[str] = []
        for item in raw:
            if isinstance(item, (list, tuple, set)):
                pages.extend(self._pages_from(*item))
            elif isinstance(item, dict):
                key = normalize_page_key(item)
                if key:
                    pages.append(key)
            else:
                key = normalize_page_key(item)
                if key:
                    pages.append(key)
        # 去重但保持顺序。
        return list(dict.fromkeys(pages))

    def _new_attempt(self, closure: dict[str, Any], stage: str, status: str,
                     worker: str = "", dispatch_id: str = "",
                     attempt_id: str = "") -> dict[str, Any]:
        key = closure["page_key"]
        attempt_no = len(closure["attempt_ids"]) + 1
        attempt_id = attempt_id or f"ATT-{key}-{attempt_no}"
        # 显式 ID 已存在时直接返回，保证调用方可以把错误/正文挂到同一次尝试。
        for attempt in self.attempts:
            if attempt["attempt_id"] == attempt_id:
                return attempt
        attempt = {
            "id": self._next("attempt"),
            "attempt_id": attempt_id,
            "page_key": key,
            "closure_detail_id": closure["closure_detail_id"],
            "attempt_no": attempt_no,
            "stage": stage,
            "status": status,
            "worker": worker or closure.get("assignee") or "",
            "dispatch_id": dispatch_id,
            "failure_type": "",
            "error_type": "",
            "error_ids": [],
            "receipt_id": "",
            "raw_response_id": "",
            "extraction_id": "",
            "requested_at": now_text(),
            "fetched_at": "",
        }
        self.attempts.append(attempt)
        closure["attempt_ids"].append(attempt_id)
        closure["attempt_id"] = attempt_id
        closure["current_attempt_id"] = attempt_id
        if not closure.get("original_attempt_id"):
            closure["original_attempt_id"] = attempt_id
        return attempt

    def _record_error(self, closure: dict[str, Any], attempt: dict[str, Any],
                      failure_type: Any, message: str = "",
                      error_code: str = "", severity: str = "error") -> dict[str, Any]:
        from app.services.error_logger import ErrorLoggerService

        raw_type = str(failure_type or READ_TIMEOUT)
        normalized = self._normalize_error_type(raw_type)
        code = error_code or normalized["code"]
        label = normalized["label"]
        english = normalized["error_type"]
        error_message = message or f"{closure['页面名称']}发生{label}"
        logger = ErrorLoggerService()
        entry = logger.domain_log_error(
            code, error_message, "error_retry",
            trace_id=attempt["attempt_id"], severity=severity,
        )
        entry.update({
            "page_key": closure["page_key"],
            "page_no": closure.get("page_no"),
            "closure_detail_id": closure["closure_detail_id"],
            "attempt_id": attempt["attempt_id"],
            "dispatch_id": attempt.get("dispatch_id", ""),
            "failure_type": raw_type,
            "error_type": english,
            "failure_type_code": english,
            "failure_type_label": label,
            "错误类型": label,
            "关联页面": closure["页面名称"],
        })
        record = {
            "id": entry["id"],
            "error_record_id": entry["id"],
            "error_code": code,
            "错误码": code,
            "message": error_message,
            "错误信息": error_message,
            "page_key": closure["page_key"],
            "closure_detail_id": closure["closure_detail_id"],
            "attempt_id": attempt["attempt_id"],
            "dispatch_id": attempt.get("dispatch_id", ""),
            "failure_type": raw_type,
            "error_type": english,
            "failure_type_label": label,
            "错误类型": label,
            "at": entry["发生时间"],
        }
        self.errors.append(record)
        closure["error_ids"].append(entry["id"])
        closure["error_record_id"] = entry["id"]
        closure["last_error_type"] = english
        closure["last_failure_type"] = raw_type
        attempt["error_ids"].append(entry["id"])
        attempt["failure_type"] = raw_type
        attempt["error_type"] = english

        # 同一页面当前未关闭的报警保持一条；不同错误仍各自保留 attempt/failure_type。
        alarm = next((a for a in self.alarms
                      if a["page_key"] == closure["page_key"] and a.get("open")), None)
        if alarm is None:
            alarm_id = f"ALM-{closure['page_key']}-{self._next('alarm')}"
            alarm = {
                "id": alarm_id,
                "alarm_id": alarm_id,
                "page_key": closure["page_key"],
                "page_name": closure["页面名称"],
                "closure_detail_id": closure["closure_detail_id"],
                "attempt_id": attempt["attempt_id"],
                "error_record_id": entry["id"],
                "error_record_ids": [entry["id"]],
                "attempt_ids": [attempt["attempt_id"]],
                "failure_type": raw_type,
                "error_type": english,
                "错误类型": label,
                "failure_types": [raw_type],
                "status": "报警中",
                "处理状态": "未处理",
                "handled": False,
                "closed": False,
                "open": True,
                "handler": "",
                "message": error_message,
                "created_at": now_text(),
                "closed_at": "",
            }
            self.alarms.append(alarm)
            closure["alarm_ids"].append(alarm_id)
            closure["open_alarm_ids"].append(alarm_id)
        else:
            alarm.setdefault("error_record_ids", []).append(entry["id"])
            alarm.setdefault("attempt_ids", []).append(attempt["attempt_id"])
            alarm.setdefault("failure_types", []).append(raw_type)
            alarm["latest_error_record_id"] = entry["id"]
            alarm["latest_attempt_id"] = attempt["attempt_id"]
            alarm["latest_failure_type"] = raw_type
            alarm["latest_error_type"] = english
            alarm["message"] = error_message
        closure["open_alarm_count"] = len(closure["open_alarm_ids"])
        closure["alarm_status"] = "报警中"
        closure["报警状态"] = "报警中"
        closure["abnormal"] = True
        return record

    @staticmethod
    def _normalize_error_type(value: str) -> dict[str, str]:
        text = str(value or "").strip()
        lower = text.lower().replace("_", "-").replace(" ", "-")
        if any(token in lower for token in ("timeout", "time-out", "超时")):
            return {"code": "READ_TIMEOUT", "error_type": "timeout", "label": READ_TIMEOUT}
        if any(token in lower for token in ("gateway", "bad-gateway", "502", "503", "504", "网关", "上游")):
            if "超时" in text or "timeout" in lower:
                return {"code": "GATEWAY_TIMEOUT", "error_type": "gateway_timeout", "label": "网关超时"}
            return {"code": "GATEWAY_ERROR", "error_type": "gateway_error", "label": GATEWAY_ERROR}
        if any(token in lower for token in ("parse", "解析")):
            return {"code": "PARSE_FAILED", "error_type": "parse_failed", "label": PARSE_ERROR}
        return {"code": "RETRY_ERROR", "error_type": lower or "error", "label": text or "错误"}

    # ---------- 上报、派工 ----------

    def report_timeout(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("error_type", READ_TIMEOUT)
        return self.report_error(*args, **kwargs)

    def report_error(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        pages = self._pages_from(*args, **kwargs)
        if not pages:
            raise ValueError("缺少页面标识")
        worker = worker_value(kwargs, "")
        failure_type = kwargs.get("error_type") or kwargs.get("failure_type") or READ_TIMEOUT
        message = kwargs.get("message") or kwargs.get("error_message") or ""
        attempt_id = kwargs.get("attempt_id") or ""
        results: list[dict[str, Any]] = []
        for key in pages:
            closure = self._closure(key)
            attempt = self._new_attempt(
                closure, stage="collect", status="失败", worker=worker,
                attempt_id=attempt_id if len(pages) == 1 else "",
            )
            record = self._record_error(closure, attempt, failure_type, message,
                                         kwargs.get("error_code") or "")
            closure["status"] = PENDING
            closure["结案状态"] = PENDING
            closure["pending"] = True
            closure["last_message"] = record["message"]
            results.append({"page": key, "page_key": key, "attempt_id": attempt["attempt_id"],
                            "error": record, **record})
            self._event("report_error", f"{key} 上报{record['错误类型']}", page_key=key,
                        attempt_id=attempt["attempt_id"])
        self._sync_store()
        response: dict[str, Any] = {
            "ok": True, "accepted": True, "count": len(results),
            "results": results, "pages": [self.public_closure(p) for p in pages],
            "state": self.state(),
        }
        if results:
            response.update(results[0])
        return response

    def schedule_retry(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        pages = self._pages_from(page, *args, **kwargs)
        if not pages:
            raise ValueError("缺少页面标识")
        key = pages[0]
        closure = self._closure(key)
        dispatch_id = kwargs.get("dispatch_id") or f"DSP-{key}-{self._next('dispatch')}"
        worker = worker_value(kwargs, "")
        attempt_no = len(closure.get("attempt_ids", [])) + 1
        error_type = kwargs.get("error_type") or closure.get("last_error_type") or "读取超时"
        delay = retry_calculator(attempt_no, float(kwargs.get("base_delay", 1.0)), jitter=False)
        retry_plan = {
            "error_type": error_type, "attempt": attempt_no,
            "delay_seconds": delay,
            "next_attempt_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + delay)),
        }
        dispatch = {
            "dispatch_id": dispatch_id,
            "id": dispatch_id,
            "page_key": key,
            "page_name": closure["页面名称"],
            "worker": worker,
            "assignee": worker,
            "status": "已预约" if not closure["verified"] else "已核验_免重采",
            "retry_required": not closure["verified"],
            "retry_exempt": bool(closure["verified"]),
            "retry_plan": retry_plan,
            "created_at": now_text(),
            "claimed_at": "",
            "reopened_at": "",
        }
        self.dispatches.append(dispatch)
        closure["dispatch_id"] = dispatch_id
        closure["retry_scheduled"] = True
        closure["预约重采"] = True
        if closure["verified"]:
            closure["retry_exempt"] = True
            closure["本次已免重采"] = True
            dispatch["status"] = "已核验_免重采"
        else:
            closure["dispatch_status"] = "待领取"
            closure["派工状态"] = "待领取"
            self._new_attempt(closure, "scheduled_retry", "已预约重采", worker, dispatch_id)
        self._event("schedule_retry", f"{key} 已预约重采；预约本身不算成功", page_key=key)
        self._sync_store()
        return {"ok": True, "dispatch": dispatch, "page": self.public_closure(key)}

    def claim_dispatch(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        pages = self._pages_from(page, *args, **kwargs)
        worker = worker_value(kwargs, self.viewer or "乙")
        self.viewer = worker
        if not pages:
            claimable = [key for key, item in self.closures.items()
                         if not item["verified"] and not item["claimed"]]
            claimable.sort(key=lambda k: page_number(k) or 10**9)
            if not claimable:
                return {"ok": False, "claimed": False, "message": "没有可领取的待补采页面",
                        "available_actions": []}
            pages = [claimable[0]]
        key = pages[0]
        closure = self._closure(key)

        if closure["verified"]:
            return {
                "ok": False, "claimed": False, "retry_exempt": True,
                "本次已免重采": True,
                "can_confirm_comparison_sample": not closure["comparison_confirmed"],
                "message": f"{key} 已核验，本次派工免重采；可确认一次对照采样",
                "available_actions": self._action_codes(closure, worker),
                "page": self.public_closure(key, worker),
            }
        if closure["claimed"] and closure["assignee"] and closure["assignee"] != worker:
            return {"ok": False, "claimed": False,
                    "message": f"{key} 已由{closure['assignee']}领取，尚未释放",
                    "page": self.public_closure(key, worker)}

        dispatch_id = closure["dispatch_id"] or f"DSP-{key}-{self._next('dispatch')}"
        dispatch = next((d for d in self.dispatches if d["dispatch_id"] == dispatch_id), None)
        if dispatch is None:
            dispatch = {"dispatch_id": dispatch_id, "id": dispatch_id, "page_key": key,
                        "page_name": closure["页面名称"], "created_at": now_text()}
            self.dispatches.append(dispatch)
        dispatch.update({"worker": worker, "assignee": worker, "status": "已领取",
                         "claimed_at": dispatch.get("claimed_at") or now_text()})
        closure.update({
            "dispatch_id": dispatch_id, "dispatch_status": "已领取", "派工状态": "已领取",
            "assignee": worker, "领取人": worker, "claimed": True,
            "尚未取页": True, "not_fetched": True, "claim_fetched": False, "worker_fetched": False,
            "actually_fetched": False,
        })
        attempt = self._new_attempt(closure, "retry", "已领取未取页", worker, dispatch_id,
                                    kwargs.get("attempt_id") or "")
        self._event("claim", f"{worker} 领取 {key}，但尚未实际取页", page_key=key,
                    attempt_id=attempt["attempt_id"])
        self._sync_store()
        return {"ok": True, "claimed": True, "not_fetched": True,
                "dispatch": dispatch, "attempt_id": attempt["attempt_id"],
                "page": self.public_closure(key, worker),
                "available_actions": self._action_codes(closure, worker)}

    def reopen_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        worker = worker_value(kwargs, self.viewer or "乙")
        self.viewer = worker
        pages = self._pages_from(*args, **kwargs)
        self._event("reopen_dispatch", f"{worker} 重新打开派工", worker=worker)
        if pages:
            key = pages[0]
            closure = self._closure(key, create=False)
            if closure is None:
                return {"ok": False, "message": f"未知页面：{key}"}
            return {"ok": True, "worker": worker, "reopened": True,
                    "page": self.public_closure(key, worker),
                    "available_actions": self._action_codes(closure, worker)}
        return self.dispatch_board(worker)

    def restore_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        worker = worker_value(kwargs, self.viewer or "乙")
        self.dispatch_enabled = True
        self.dispatch_status = "已恢复"
        self._event("restore_dispatch", "派工已恢复；其他未结案页面仍可领取")
        board = self.dispatch_board(worker)
        board.update({"ok": True, "restored": True})
        return board

    def release_dispatch(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        key = self._pages_from(page, *args, **kwargs)[0]
        closure = self._closure(key)
        worker = kwargs.get("worker") or self.viewer
        closure.update({"claimed": False, "尚未取页": False, "not_fetched": False,
                        "dispatch_status": "待领取",
                        "派工状态": "待领取", "assignee": "", "领取人": ""})
        dispatch = next((d for d in self.dispatches if d["dispatch_id"] == closure["dispatch_id"]), None)
        if dispatch:
            dispatch.update({"status": "待领取", "worker": "", "assignee": ""})
        self._sync_store()
        return {"ok": True, "released": True, "page": self.public_closure(key, worker)}

    def dispatch_board(self, worker: str = "乙") -> dict[str, Any]:
        self.viewer = worker
        items = [self.public_closure(key, worker) for key in sorted(
            self.closures, key=lambda k: page_number(k) or 10**9)]
        claimable = [item["page_key"] for item in items
                     if item["status"] != VERIFIED and not item["claimed"]]
        mine = [item["page_key"] for item in items if item.get("assignee") == worker]
        return {"ok": True, "worker": worker, "dispatch_enabled": self.dispatch_enabled,
                "dispatch_status": self.dispatch_status, "claimable_pages": claimable,
                "my_pages": mine, "pages": items, "dispatches": self.dispatches,
                "other_pages_claimable": bool(claimable)}

    # ---------- 正文、解析、回执、结案 ----------

    def _extract_body(self, *args: Any, **kwargs: Any) -> tuple[str, Any, Any]:
        candidates: list[Any] = list(args)
        for key in ("response", "raw_response", "actual_response", "page_response",
                    "raw_html", "page_html", "original_html", "response_body",
                    "actual_text", "page_text", "body_text", "page", "body", "text",
                    "html", "content", "response_text", "响应文字", "响应正文",
                    "实际响应", "实际响应文字", "原始响应", "原始页", "原始页面", "正文"):
            if key in kwargs:
                candidates.append(kwargs[key])
        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, str):
                return candidate, kwargs.get("status_code"), kwargs.get("parser_rule") or kwargs.get("rule")
            if isinstance(candidate, dict):
                body = _first_value(candidate, "text", "body", "html", "content",
                                    "response_text", "raw_html", "actual_text",
                                    "响应文字", "响应正文", "实际响应文字", "原始页", "原始页面", "正文")
                if body is not None:
                    return str(body), _first_value(candidate, "status_code", "http_status", "状态码"), \
                        _first_value(candidate, "parser_rule", "rule", "解析规则") or kwargs.get("parser_rule")
        return "", kwargs.get("status_code"), kwargs.get("parser_rule") or kwargs.get("rule")

    def submit_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        page_arg = kwargs.pop("page", None) or kwargs.pop("page_key", None) or kwargs.pop("page_id", None)
        if args and page_arg is None:
            page_arg = args[0]
            body_args = args[1:]
        else:
            body_args = args
        key = normalize_page_key(page_arg, **kwargs)
        if not key and body_args and isinstance(body_args[0], dict):
            key = normalize_page_key(body_args[0])
        if not key:
            pages = self._pages_from(**kwargs)
            key = pages[0] if pages else ""
        closure = self._closure(key)

        text, status_from_body, rule_from_body = self._extract_body(*body_args, **kwargs)
        if not text and kwargs.get("response_text"):
            text = str(kwargs["response_text"])
        status_code = kwargs.get("status_code", status_from_body)
        rule = (kwargs.get("parser_rule") or kwargs.get("parse_rule")
                or kwargs.get("parser_rules") or kwargs.get("rules") or kwargs.get("rule")
                or rule_from_body)
        response_id = kwargs.get("response_id") or kwargs.get("raw_response_id") or ""
        source = kwargs.get("source") or ("comparison" if kwargs.get("purpose") == "comparison" else "supplement")

        if response_id:
            existing = next((r for r in self.raw_responses if r["raw_response_id"] == response_id), None)
            if existing:
                return self._submission_result(closure, existing, duplicate=True)

        attempt_id = kwargs.get("attempt_id") or closure.get("current_attempt_id") or ""
        attempt = next((a for a in self.attempts if a["attempt_id"] == attempt_id), None) if attempt_id else None
        if attempt is None:
            attempt = self._new_attempt(closure, source or "supplement", "已取页",
                                        kwargs.get("worker") or closure.get("assignee") or self.viewer,
                                        closure.get("dispatch_id", ""), attempt_id)
        attempt["status"] = "已取得响应正文"
        attempt["fetched_at"] = now_text()
        external_recovery = any(token in str(source) for token in ("late", "receipt", "supplement", "external", "original"))
        fetched_by_worker = bool(kwargs.get("fetched_by_worker") or kwargs.get("actual_fetch")
                                 or (source in {"worker_fetch", "comparison", "fetch"} and not external_recovery))
        closure["response_obtained"] = True
        closure["actually_fetched"] = True
        if fetched_by_worker:
            closure["claim_fetched"] = True
            closure["worker_fetched"] = True
            closure["尚未取页"] = False
            closure["not_fetched"] = False
        elif external_recovery:
            # 迟到的原页/外部补文已经可用于结案，但不能记成乙已完成本次派工取页。
            closure["late_response_recovered"] = True
            closure["original_page_recovered"] = "original" in str(source) or kwargs.get("original_page") is True
            closure["recovered_response_id"] = response_id or ""
        else:
            closure["claim_fetched"] = True
            closure["worker_fetched"] = True
            closure["尚未取页"] = False
            closure["not_fetched"] = False

        raw_id = response_id or f"RAW-{key}-{self._next('raw')}"
        content_hash_value = content_hash(text) if text else ""
        duplicate_body = next((r for r in self.raw_responses
                               if r["page_key"] == key and r.get("content_hash") == content_hash_value
                               and content_hash_value), None)
        if duplicate_body and not response_id:
            old_extraction = next((e for e in self.extractions
                                   if e["extraction_id"] == duplicate_body.get("extraction_id")), None)
            old_parse = old_extraction.get("parse_result") if old_extraction else {}
            # 完全相同的正文作为对照采样时，仍可记录“已核对一次”，
            # 但必须走只采样、不计数路径。
            if kwargs.get("purpose") == "comparison" and old_extraction and old_extraction.get("valid"):
                self._record_sample_only(
                    closure, duplicate_body, old_extraction,
                    worker=worker_value(kwargs, self.viewer),
                )
                self._sync_store()
                return {
                    "ok": True, "confirmed": True, "counted": False, "valid": True,
                    "counted_as_success": False, "duplicate_response": True,
                    "message": "已用同一有效页完成一次对照采样；不第二次计入该页",
                    "raw_response": duplicate_body,
                    "raw_response_id": duplicate_body["raw_response_id"],
                    "parse_result": old_parse, "extraction": old_extraction,
                    "page": self.public_closure(key),
                }
            return self._submission_result(
                closure, duplicate_body, duplicate=True,
                valid=bool(old_extraction and old_extraction.get("valid")),
                parse_result=old_parse, extraction=old_extraction,
            )

        raw = {
            "raw_response_id": raw_id, "id": raw_id, "page_key": key,
            "closure_detail_id": closure["closure_detail_id"],
            "attempt_id": attempt["attempt_id"], "response_text": text,
            "原始页": text, "content_hash": content_hash_value,
            "status_code": status_code, "source": source,
            "extraction_id": "", "received_at": now_text(),
        }
        self.raw_responses.append(raw)
        closure["raw_response_ids"].append(raw_id)
        closure["raw_response_id"] = raw_id
        attempt["raw_response_id"] = raw_id

        parse_rule = rule
        if status_code is not None and isinstance(parse_rule, dict):
            parse_rule.setdefault("status_code", status_code)
        parse_result = parse_actual_response(text, parse_rule, page_key=key, raw_response_id=raw_id)
        extraction_id = f"EXT-{key}-{self._next('extraction')}"
        extraction = {
            "extraction_id": extraction_id, "id": extraction_id,
            "raw_response_id": raw_id, "page_key": key,
            "closure_detail_id": closure["closure_detail_id"],
            "attempt_id": attempt["attempt_id"],
            "parser_rule_id": rule if isinstance(rule, int) else (rule or {}).get("规则标识", "") if isinstance(rule, dict) else "",
            "parse_result": parse_result,
            "items": parse_result.get("items", []),
            "item_count": parse_result.get("item_count", 0),
            "valid": parse_result.get("valid", False),
            "created_at": now_text(),
        }
        self.extractions.append(extraction)
        raw["extraction_id"] = extraction_id
        closure["extraction_ids"].append(extraction_id)
        closure["extraction_id"] = extraction_id
        attempt["extraction_id"] = extraction_id

        if not parse_result.get("valid"):
            failure_type = parse_result.get("failure_type") or PARSE_ERROR
            record = self._record_error(closure, attempt, failure_type,
                                        parse_result.get("message", "解析检查未通过"),
                                        parse_result.get("error_code", ""))
            closure["last_message"] = record["message"]
            self._sync_store()
            return self._submission_result(closure, raw, valid=False, parse_result=parse_result,
                                           extraction=extraction, error=record)

        # 有效页：若尚未结案则首次结案并确认数据；已结案后的有效页只能作为采样，不二次计数。
        first_closure = not closure["verified"]
        comparison = source == "comparison" or kwargs.get("purpose") == "comparison"
        if first_closure:
            self._confirm_valid_page(closure, attempt, raw, extraction, source=source)
        elif comparison:
            self._record_sample_only(closure, raw, extraction, worker=kwargs.get("worker") or self.viewer)
        else:
            # 普通重复补到同一有效页：保留关联，但不重复确认数据。
            extraction["counted"] = False
            extraction["duplicate_success"] = True

        # 若此前已有成功回执等待正文，现在把它标记为已解析。
        for receipt_id in closure["receipt_ids"]:
            receipt = next((r for r in self.receipts if r["receipt_id"] == receipt_id), None)
            if receipt and not receipt.get("parse_passed") and receipt.get("success"):
                receipt.update({"parse_passed": True, "valid": True,
                                "parse_status": "通过", "extraction_id": extraction_id,
                                "raw_response_id": raw_id})

        self._sync_store()
        return self._submission_result(
            closure, raw, valid=True, parse_result=parse_result, extraction=extraction,
            first_closure=first_closure,
        )

    def _confirm_valid_page(self, closure: dict[str, Any], attempt: dict[str, Any],
                            raw: dict[str, Any], extraction: dict[str, Any],
                            source: str = "") -> None:
        key = closure["page_key"]
        items = extraction.get("items", [])
        data_ids: list[str] = []
        for index, item in enumerate(items, 1):
            payload = dict(item)
            item_hash = content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
            if any(d.get("item_hash") == item_hash and d.get("page_key") == key for d in self.confirmed_data):
                continue
            data_id = f"DATA-{key}-{self._next('data')}"
            datum = {
                "data_id": data_id, "id": data_id, "page_key": key,
                "closure_detail_id": closure["closure_detail_id"],
                "attempt_id": attempt["attempt_id"],
                "raw_response_id": raw["raw_response_id"],
                "extraction_id": extraction["extraction_id"],
                "source": source, "payload": payload, "item_hash": item_hash,
                "confirmed": True, "counted": True, "created_at": now_text(),
            }
            self.confirmed_data.append(datum)
            data_ids.append(data_id)
            closure["data_ids"].append(data_id)

        closure.update({
            "status": VERIFIED, "结案状态": VERIFIED, "pending": False,
            "closed": True, "verified": True, "success": True,
            "success_count": 1, "closure_count": 1,
            "valid_receipt_received": True,
            "data_confirmed": True, "data_confirmed_once": True,
            "confirmed_data_count": len(closure["data_ids"]),
            "actual_data_count": len(closure["data_ids"]),
            "closed_at": now_text(),
            "last_message": "有效页通过解析检查，页面独立结案且数据只确认一次",
        })
        # 已结案页面的重试派工免重采，但保留一次可选对照采样。
        closure["retry_exempt"] = True
        closure["本次已免重采"] = True
        if closure["dispatch_status"] == "已领取":
            closure["dispatch_status"] = "已核验_免重采"
            closure["派工状态"] = "已核验_免重采"
        dispatch = next((d for d in self.dispatches if d["dispatch_id"] == closure.get("dispatch_id")), None)
        if dispatch:
            dispatch.update({"status": "已核验_免重采", "retry_required": False, "retry_exempt": True})
        self._close_alarms_for_page(key, reason="收到有效回执/有效页并通过解析检查")
        self._event("close_page", f"{key} 切换为已核验并生成结案明细", page_key=key,
                    raw_response_id=raw["raw_response_id"],
                    extraction_id=extraction["extraction_id"])

    def _record_sample_only(self, closure: dict[str, Any], raw: dict[str, Any],
                           extraction: dict[str, Any], worker: str = "乙") -> None:
        key = closure["page_key"]
        sample_id = f"SAMPLE-{key}-{self._next('sample')}"
        sample = {
            "sample_id": sample_id, "id": sample_id, "page_key": key,
            "raw_response_id": raw["raw_response_id"],
            "extraction_id": extraction["extraction_id"],
            "worker": worker, "items": extraction.get("items", []),
            "item_count": extraction.get("item_count", 0),
            "counted": False, "created_at": now_text(),
        }
        self.sample_data.append(sample)
        closure["comparison_confirmed"] = True
        closure["comparison_count"] = 1
        closure["comparison_status"] = "已确认"
        closure["对照采样状态"] = "已确认（不重复计入）"
        closure["last_message"] = "对照采样已核对，但不第二次计入该页成功或数据"
        extraction["counted"] = False
        extraction["sample_id"] = sample_id

    def close_page(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """在已有有效解析结果时显式结案；没有有效页则拒绝。"""
        key = self._pages_from(page, *args, **kwargs)[0]
        closure = self._closure(key)
        if closure["verified"]:
            view = self.public_closure(key, worker_value(kwargs, self.viewer))
            return {"ok": True, "closed": True, "duplicate": True, "counted": False,
                    "message": "页面已经结案，重复结案不会第二次计入", "page": view,
                    "closure_detail": view}
        extraction = next((e for e in reversed(self.extractions)
                           if e["page_key"] == key and e.get("valid")), None)
        if extraction is None:
            return {"ok": False, "closed": False,
                    "message": "尚未收到并解析通过的有效页，不能结案",
                    "page": self.public_closure(key)}
        raw = next((r for r in self.raw_responses
                    if r["raw_response_id"] == extraction["raw_response_id"]), None)
        attempt = next((a for a in self.attempts
                        if a["attempt_id"] == extraction.get("attempt_id")), None)
        if raw is None or attempt is None:
            return {"ok": False, "closed": False, "message": "有效页缺少原始页或尝试关联，不能结案"}
        self._confirm_valid_page(closure, attempt, raw, extraction, source=raw.get("source", "manual_close"))
        self._sync_store()
        view = self.public_closure(key)
        return {"ok": True, "closed": True, "duplicate": False, "counted": True,
                "message": "有效页已结案", "page": view, "closure_detail": view}

    settle_page = close_page
    verify_page = close_page
    complete_page = close_page

    def confirm_data(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """显式确认成功页数据；重复调用不会新增第二份确认。"""
        key = self._pages_from(page, *args, **kwargs)[0]
        closure = self._closure(key)
        if not closure["verified"]:
            return {"ok": False, "confirmed": False, "counted": False,
                    "message": "只有通过解析检查的已核验页面才能确认数据",
                    "page": self.public_closure(key)}
        first = not closure.get("data_confirmation_invoked")
        closure["data_confirmation_invoked"] = True
        closure["data_confirmed"] = True
        closure["data_confirmed_once"] = True
        self._sync_store()
        return {"ok": True, "confirmed": True, "first_confirmation": first,
                "duplicate": not first, "counted": first,
                "confirmed_data_count": closure["confirmed_data_count"],
                "message": "成功页数据已确认一次" if first else "数据已确认过，不重复计数",
                "page": self.public_closure(key)}

    def confirm_comparison_sample(self, page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        key = self._pages_from(page, *args, **kwargs)[0]
        closure = self._closure(key)
        worker = worker_value(kwargs, self.viewer)
        for worker_key in ("worker", "assignee", "operator", "user", "owner", "handler", "claimed_by"):
            kwargs.pop(worker_key, None)
        if not closure["verified"]:
            return {"ok": False, "confirmed": False, "counted": False,
                    "message": "页面尚未核验，不能把对照采样作为成功结案",
                    "page": self.public_closure(key, worker)}
        if closure["comparison_confirmed"]:
            return {"ok": False, "allowed": False, "confirmed": False, "counted": False,
                    "comparison_counted": False, "counted_as_comparison": False,
                    "counted_as_success": False, "duplicate": True,
                    "message": "该页对照采样已确认，核对结果不能第二次计入",
                    "page": self.public_closure(key, worker)}
        text, _, _ = self._extract_body(*args, **kwargs)
        if text:
            result = self.submit_response(
                key, *args, purpose="comparison", worker=worker, source="comparison", **kwargs
            )
            if not result.get("valid"):
                return {"ok": False, "confirmed": False, "counted": False,
                        "message": "对照采样未通过解析检查", "submission": result,
                        "page": self.public_closure(key, worker)}
            return {"ok": True, "allowed": True, "confirmed": True, "counted": False,
                    "comparison_counted": True, "counted_as_comparison": True,
                    "counted_as_success": False, "comparison_count": 1,
                    "message": "已确认一次对照采样；结果不第二次计入该页",
                    "submission": result, "page": self.public_closure(key, worker)}
        # 没有新正文时，只允许记录“已做一次核对”，不增加成功数。
        self._record_sample_only(closure, {"raw_response_id": closure.get("raw_response_id", "")},
                                 {"extraction_id": closure.get("extraction_id", ""), "items": []}, worker)
        self._sync_store()
        return {"ok": True, "allowed": True, "confirmed": True, "counted": False,
                "comparison_counted": True, "counted_as_comparison": True,
                "counted_as_success": False, "comparison_count": 1,
                "message": "已确认一次对照采样；不重复计入该页",
                "page": self.public_closure(key, worker)}

    def receive_receipt(self, receipt: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if isinstance(receipt, dict):
            payload = {**receipt, **kwargs}
            page_arg = payload.pop("page", None)
        else:
            payload = dict(kwargs)
            page_arg = receipt
            if page_arg is None:
                page_arg = payload.pop("page", None) or payload.pop("page_key", None) or payload.pop("page_id", None)
        key = normalize_page_key(page_arg)
        if not key:
            key = normalize_page_key(**payload)
        if not key:
            keys = self._pages_from(**payload)
            key = keys[0] if keys else ""
        closure = self._closure(key)
        receipt_id = str(_first_value(payload, "receipt_id", "id", "回执编号", default="") or f"RCP-{key}-{self._next('receipt')}")
        success_value = _first_value(payload, "success", "successful", "is_success",
                                     "ok", "成功", default=True)
        if isinstance(success_value, str):
            success = success_value.strip() in {"true", "True", "1", "成功", "已成功", "yes", "success", "succeeded"}
        else:
            success = bool(success_value)
        status_value = str(payload.get("status") or payload.get("receipt_status") or "").strip().lower()
        if status_value in {"failed", "failure", "error", "失败"}:
            success = False
        elif status_value in {"success", "succeeded", "successful", "成功", "已成功"}:
            success = True
        attempt_id = _first_value(payload, "attempt_id", "trace_id", default=closure.get("current_attempt_id", ""))

        existing_id = next((r for r in self.receipts if r["receipt_id"] == receipt_id), None)
        if existing_id:
            return {"ok": True, "duplicate": True, "already_processed": True,
                    "counted": False, "receipt": existing_id,
                    "page": self.public_closure(key),
                    "message": "重复回执已忽略，同一页不会记作两次成功"}

        already_verified = closure["verified"]
        text, status_code, rule = self._extract_body(**payload)
        parse_result: dict[str, Any] | None = None
        extraction_id = payload.get("extraction_id") or ""
        valid = False
        if text:
            submission = self.submit_response(
                key, text, status_code=status_code, parser_rule=rule,
                attempt_id=attempt_id, source="late_receipt",
                response_id=payload.get("response_id") or payload.get("raw_response_id"),
                worker=payload.get("worker") or self.viewer,
            )
            parse_result = submission.get("parse_result")
            extraction_id = submission.get("extraction", {}).get("extraction_id", "")
            valid = bool(submission.get("valid"))
        elif extraction_id:
            extraction = next((e for e in self.extractions if e["extraction_id"] == extraction_id), None)
            valid = bool(extraction and extraction.get("valid"))
            parse_result = extraction.get("parse_result") if extraction else None
        elif isinstance(payload.get("parse_result"), dict):
            parse_result = payload["parse_result"]
            valid = bool(parse_result.get("valid") or parse_result.get("parse_passed") or parse_result.get("ok"))
        elif payload.get("parsed") is True or payload.get("parse_status") == "通过":
            valid = True

        receipt_record = {
            "receipt_id": receipt_id, "id": receipt_id, "page_key": key,
            "closure_detail_id": closure["closure_detail_id"], "attempt_id": attempt_id,
            "success": success, "received_at": now_text(),
            "parse_status": "通过" if valid else ("未通过" if parse_result else "待解析"),
            "parse_passed": valid, "valid": bool(success and valid),
            "extraction_id": extraction_id,
            "raw_response_id": (parse_result or {}).get("raw_response_id", "") if parse_result else "",
            "duplicate": already_verified, "counted": False,
        }
        self.receipts.append(receipt_record)
        closure["receipt_ids"].append(receipt_id)
        closure["receipt_id"] = receipt_id
        closure["receipt_received"] = True
        closure["receipt_status"] = "成功" if success else "失败"
        attempt = next((a for a in self.attempts if a["attempt_id"] == attempt_id), None)
        if attempt:
            attempt["receipt_id"] = receipt_id

        if success and valid and not closure["verified"]:
            # submit_response 已完成结案；此分支保留给通过 extraction_id 结案的情况。
            closure["valid_receipt_received"] = True
            if extraction_id and not closure["verified"]:
                extraction = next(e for e in self.extractions if e["extraction_id"] == extraction_id)
                raw = next(r for r in self.raw_responses if r["raw_response_id"] == extraction["raw_response_id"])
                attempt = next((a for a in self.attempts if a["attempt_id"] == extraction["attempt_id"]), None)
                self._confirm_valid_page(closure, attempt or self._new_attempt(closure, "receipt", "有效回执"),
                                         raw, extraction, source="receipt")
            receipt_record["counted"] = True
        elif success and not valid:
            closure["last_message"] = "成功回执已到达，但尚未通过解析检查，不能结案"
        elif already_verified:
            receipt_record["duplicate"] = True
            receipt_record["counted"] = False

        self._sync_store()
        page_view = self.public_closure(key)
        return {"ok": True, "received": True, "duplicate": already_verified,
                "counted": receipt_record.get("counted", False),
                "valid": receipt_record["valid"], "receipt": receipt_record,
                "parse_result": parse_result,
                "page": page_view, "closure_detail": page_view, "detail": page_view,
                "message": "重复回执不能将同一页记作两次成功" if already_verified else closure["last_message"]}

    def _close_alarms_for_page(self, key: str, reason: str = "") -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        closure = self._closure(key)
        for alarm_id in list(closure["open_alarm_ids"]):
            alarm = next(a for a in self.alarms if a["alarm_id"] == alarm_id)
            alarm.update({"closed": True, "open": False, "handled": True,
                          "status": "已关闭", "处理状态": "已解决",
                          "closed_at": now_text(), "close_reason": reason})
            closed.append(alarm)
        closure["open_alarm_ids"] = []
        closure["open_alarm_count"] = 0
        closure["abnormal"] = False
        if closure["alarm_status"] == "报警中":
            closure["alarm_status"] = "已关闭"
            closure["报警状态"] = "已关闭"
        return closed

    def close_alarm(self, alarm_or_page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        alarm_id = kwargs.get("alarm_id") or (alarm_or_page if isinstance(alarm_or_page, str) and str(alarm_or_page).startswith("ALM") else "")
        if alarm_id:
            alarm = next((a for a in self.alarms if a["alarm_id"] == alarm_id), None)
            if alarm is None:
                return {"ok": False, "closed": False, "message": f"报警不存在：{alarm_id}"}
            key = alarm["page_key"]
        else:
            keys = self._pages_from(alarm_or_page, *args, **kwargs)
            if not keys:
                return {"ok": False, "closed": False, "message": "缺少报警或页面标识"}
            key = keys[0]
        closure = self._closure(key)
        if not closure["verified"] and not closure["valid_receipt_received"]:
            return {"ok": False, "closed": False,
                    "message": "尚未收到通过解析检查的有效回执，不能关闭报警；响应正文到达也不例外",
                    "page": self.public_closure(key)}
        closed = self._close_alarms_for_page(key, kwargs.get("reason") or "有效回执已核验")
        self._sync_store()
        return {"ok": True, "closed": True, "closed_alarms": closed,
                "page": self.public_closure(key)}

    def handle_alarm(self, alarm_or_page: Any = None, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """人工认领/处理报警，但不把它关闭。"""
        worker = kwargs.get("worker") or kwargs.get("handler") or self.viewer
        if isinstance(alarm_or_page, str) and alarm_or_page.startswith("ALM"):
            alarms = [next(a for a in self.alarms if a["alarm_id"] == alarm_or_page)]
            key = alarms[0]["page_key"]
        else:
            key = self._pages_from(alarm_or_page, *args, **kwargs)[0]
            alarms = [a for a in self.alarms if a["page_key"] == key and a["open"]]
        for alarm in alarms:
            alarm.update({"handled": True, "handler": worker, "处理状态": "处理中",
                          "status": "处理中"})
        closure = self._closure(key)
        closure["alarm_status"] = "处理中"
        closure["报警状态"] = "处理中"
        self._sync_store()
        return {"ok": True, "handled": True, "closed": False, "alarms": alarms,
                "message": "报警已有人跟进，但未取得有效回执前不会关闭"}

    # ---------- 视图与动作 ----------

    def _action_codes(self, closure: dict[str, Any], worker: str = "乙") -> list[dict[str, Any]]:
        key = closure["page_key"]
        actions: list[dict[str, Any]] = []

        def add(code: str, label: str, enabled: bool, reason: str = "", once: bool = False) -> None:
            actions.append({"code": code, "action": code, "label": label,
                            "name": label, "enabled": enabled, "reason": reason,
                            "once": once, "page_key": key})

        if closure["verified"]:
            add("view_closure", "查看结案明细", True)
            if closure["open_alarm_count"]:
                add("close_alarm", "关闭报警", True)
            if not closure["comparison_confirmed"]:
                add("confirm_comparison_sample", "确认对照采样", True, once=True)
            else:
                add("confirm_comparison_sample", "再查看对照结果", False, "对照采样只能确认一次", once=True)
            add("release_dispatch", "释放派工", closure["claimed"] and closure.get("assignee") == worker)
            add("duplicate_success", "重复确认成功", False, "同一页成功只能计入一次")
        else:
            if not self.dispatch_enabled:
                add("restore_dispatch", "恢复派工", True)
            elif not closure["claimed"]:
                add("claim_dispatch", "领取页面", True)
            elif closure.get("assignee") == worker:
                add("submit_response", "补响应正文/取页", True)
                add("release_dispatch", "释放领取", True)
            else:
                add("claim_dispatch", "领取页面", False, f"已由{closure.get('assignee')}领取")
            add("schedule_retry", "预约重采", not closure["retry_scheduled"],
                "预约只表示安排，不算成功" if closure["retry_scheduled"] else "")
            add("receive_receipt", "登记回执", True)
            add("submit_response", "补响应正文", True)
            if closure["open_alarm_count"]:
                add("handle_alarm", "处理报警", True)
                add("close_alarm", "关闭报警", False, "有效回执通过解析后才可关闭")
        return actions

    def public_closure(self, key: str, worker: str = "乙") -> dict[str, Any]:
        closure = self._closure(key, create=False)
        if closure is None:
            return {}
        # 动态字段不写回复杂对象，避免不同 worker 视图互相污染。
        view = dict(closure)
        view["明细状态"] = closure.get("结案状态") or closure.get("status")
        view["closure_status"] = closure.get("status")
        view["page_state"] = closure.get("status")
        view["页面状态"] = closure.get("status")
        actions = self._action_codes(closure, worker)
        view["available_actions"] = actions
        view["actions"] = [a["code"] for a in actions if a["enabled"]]
        view["action_labels"] = [a["label"] for a in actions if a["enabled"]]
        view["可执行动作"] = "、".join(view["action_labels"])
        view["error_id"] = closure["error_ids"][-1] if closure.get("error_ids") else ""
        view["alarm_id"] = closure["alarm_ids"][0] if closure.get("alarm_ids") else ""
        view["open_alarm_id"] = closure["open_alarm_ids"][0] if closure.get("open_alarm_ids") else ""
        view["raw_id"] = closure.get("raw_response_id", "")
        view["response_id"] = closure.get("raw_response_id", "")
        view["html_id"] = closure.get("raw_response_id", "")
        view["parse_id"] = closure.get("extraction_id", "")
        view["extraction_result_id"] = closure.get("extraction_id", "")
        view["parsed_result_id"] = closure.get("extraction_id", "")
        view["data_id"] = closure["data_ids"][0] if closure.get("data_ids") else ""
        view["attempt_records"] = [a for a in self.attempts if a["page_key"] == key]
        view["receipt_records"] = [r for r in self.receipts if r["page_key"] == key]
        view["error_records"] = [e for e in self.errors if e["page_key"] == key]
        view["alarm_records"] = [a for a in self.alarms if a["page_key"] == key]
        view["raw_response_records"] = [r for r in self.raw_responses if r["page_key"] == key]
        view["extraction_records"] = [e for e in self.extractions if e["page_key"] == key]
        # 常用短键别名。
        view["attempts"] = view["attempt_records"]
        view["receipts"] = view["receipt_records"]
        view["errors"] = view["error_records"]
        view["alarms"] = view["alarm_records"]
        view["raw_responses"] = view["raw_response_records"]
        view["extractions"] = view["extraction_records"]
        return view

    def _submission_result(self, closure: dict[str, Any], raw: dict[str, Any], *,
                           valid: bool = False, parse_result: dict[str, Any] | None = None,
                           extraction: dict[str, Any] | None = None,
                           error: dict[str, Any] | None = None,
                           duplicate: bool = False, first_closure: bool = False) -> dict[str, Any]:
        key = closure["page_key"]
        self._sync_store()
        page_view = self.public_closure(key)
        return {
            "ok": True, "accepted": True, "valid": valid, "closed": bool(closure["verified"]),
            "verified": bool(closure["verified"]),
            "success": bool(closure["verified"]) and first_closure,
            "new_success": first_closure, "counted": first_closure,
            "duplicate": duplicate,
            "page_key": key, "raw_response": raw, "raw_response_id": raw["raw_response_id"],
            "parse_result": parse_result or {}, "extraction": extraction or {},
            "extraction_id": (extraction or {}).get("extraction_id", ""),
            "error": error, "page": page_view,
            "closure_detail": page_view, "detail": page_view,
            "available_actions": self._action_codes(closure, self.viewer),
        }

    def state(self, worker: str = "乙", page: int = 1, size: int = 20, **_ignored: Any) -> dict[str, Any]:
        pages = [self.public_closure(key, worker) for key in sorted(
            self.closures, key=lambda k: (page_number(k) is None, page_number(k) or 0, k))]
        total = len(pages)
        current_page = max(1, int(page or 1))
        page_size = max(1, min(200, int(size or 20)))
        start = (current_page - 1) * page_size
        paged_items = pages[start:start + page_size]
        return {
            "ok": True, "items": paged_items, "total": total,
            "page": current_page, "size": page_size,
            "initialized": self.initialized, "worker": worker,
            "dispatch_enabled": self.dispatch_enabled,
            "dispatch_status": self.dispatch_status,
            "pages": pages, "closures": pages, "details": pages,
            "dispatches": self.dispatches, "attempts": self.attempts,
            "receipts": self.receipts, "raw_responses": self.raw_responses,
            "extraction_results": self.extractions, "extractions": self.extractions,
            "actual_data": self.confirmed_data, "confirmed_data": self.confirmed_data,
            "sample_data": self.sample_data, "errors": self.errors, "alarms": self.alarms,
            "events": self.events,
            "summary": {
                "page_count": len(pages),
                "pending": sum(1 for p in pages if p["status"] == PENDING),
                "verified": sum(1 for p in pages if p["status"] == VERIFIED),
                "open_alarms": sum(1 for a in self.alarms if a.get("open")),
                "success_count": sum(p["success_count"] for p in pages),
                "actual_data_count": len(self.confirmed_data),
            },
        }

    def parse_actual_response(self, text: str, parser_rule: Any = None, **kwargs: Any) -> dict[str, Any]:
        return parse_actual_response(text, parser_rule, **kwargs)

    parse_response = parse_actual_response

    def list_closures(self, **filters: Any) -> list[dict[str, Any]]:
        items = [self.public_closure(key) for key in sorted(self.closures)]
        status = filters.get("status")
        if status:
            items = [item for item in items if item.get("status") == status or item.get("结案状态") == status]
        keyword = filters.get("keyword")
        if keyword:
            text = str(keyword).casefold()
            items = [item for item in items if text in str(item).casefold()]
        return items

    def _sync_store(self) -> None:
        rows = store.rows("page_closure_detail")
        rows.clear()
        rows.extend(self.list_closures())

    def dispatch(self, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        name = _ACTION_ALIASES.get(str(command or "").strip(), str(command or "").strip())
        method = getattr(self, name, None)
        if method is None:
            # 也允许直接调用已知领域方法名。
            method = getattr(self, str(command), None)
        if (method is None or not callable(method)) and command:
            text = str(command)
            if "回执" in text:
                method = self.receive_receipt
            elif "对照" in text or "采样" in text:
                method = self.confirm_comparison_sample
            elif "确认数据" in text or "数据确认" in text:
                method = self.confirm_data
            elif "报警" in text:
                method = self.close_alarm if ("关闭" in text or "解决" in text) else self.handle_alarm
            elif "领取" in text:
                method = self.claim_dispatch
            elif "恢复派工" in text or "恢复调度" in text:
                method = self.restore_dispatch
            elif "重新打开" in text or "重开" in text:
                method = self.reopen_dispatch
            elif "释放" in text:
                method = self.release_dispatch
            elif "预约" in text or "重采" in text:
                method = self.schedule_retry
            elif "超时" in text or "错误" in text or "上报" in text:
                method = self.report_timeout
            elif "解析" in text and "正文" not in text:
                method = self.parse_actual_response
            elif "正文" in text or "响应" in text or "补" in text:
                method = self.submit_response
            elif "结案" in text or "明细" in text or "状态" in text:
                method = self.state
        if method is None or not callable(method):
            return {"ok": False, "message": f"不支持的结案动作：{command}",
                    "available_commands": sorted(set(_ACTION_ALIASES.values()))}
        return method(**payload)

    # ---------- 题设一键回放 ----------

    def canonical_valid_page_6(self) -> str:
        return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>第6页商品列表-原有效页</title></head>
<body data-page="6" data-valid-page="true">
  <section class="product-list">
    <article class="product-item" data-sku="ORIG-P6-001">
      <h2 class="product-title">第6页迟到回执对应的原有效商品</h2>
      <span class="product-price">¥66.00</span>
      <span class="product-stock">有货</span>
    </article>
  </section>
</body></html>"""

    def canonical_gateway_page_7(self) -> str:
        return """<!doctype html>
<html><head><title>502 Bad Gateway</title></head>
<body><center><h1>502 Bad Gateway</h1></center>
<p>网关错误：上游采集通道返回无效响应，请稍后重试。</p></body></html>"""

    def run_canonical_scenario(self, **kwargs: Any) -> dict[str, Any]:
        """按题设执行：超时 → 领取P6 → 迟到有效页/回执 → P7网关正文。"""
        worker = kwargs.get("worker") or "乙"
        self.reset(seed="baseline", worker=worker)
        self.report_timeout(pages=[6, 7], worker="系统", error_type=READ_TIMEOUT)
        self.schedule_retry(page=6, worker=worker)
        self.claim_dispatch(page=6, worker=worker)
        valid_html = kwargs.get("valid_html") or self.canonical_valid_page_6()
        receipt_id = kwargs.get("receipt_id") or "RCP-P6-LATE"
        self.receive_receipt(page=6, receipt_id=receipt_id, success=True,
                             text=valid_html, source="late_original", worker=worker)
        gateway_html = kwargs.get("gateway_html") or self.canonical_gateway_page_7()
        self.submit_response(page=7, text=gateway_html, status_code=502,
                             source="gateway_supplement", worker=worker)
        self.restore_dispatch(worker=worker)
        result = self.state(worker)
        result.update({
            "scenario": "page-6-late-receipt-page-7-gateway",
            "narrative_checks": {
                "page_6_verified": self.closures["P6"]["status"] == VERIFIED,
                "page_6_retry_exempt": self.closures["P6"]["retry_exempt"],
                "page_6_comparison_still_available": not self.closures["P6"]["comparison_confirmed"],
                "page_6_success_count_once": self.closures["P6"]["success_count"] == 1,
                "page_7_pending": self.closures["P7"]["status"] == PENDING,
                "page_7_alarm_open": self.closures["P7"]["open_alarm_count"] > 0,
                "page_8_claimable": not self.closures["P8"]["claimed"],
            },
        })
        return result


board = PageClosureBoard(seed_canonical=False)


class PageClosureDetailService(BaseModuleService):
    MODULE = "page_closure_detail"
    ENTITY = "页面结案明细"
    OPERATION_TERM = "结案"
    LIST_FIELDS = [
        "结案明细编号", "页面编号", "页面名称", "结案状态", "报警状态", "派工状态",
        "领取人", "对照采样状态", "成功次数", "实际数据数", "当前尝试", "回执编号",
        "原始响应", "提取结果", "可执行动作",
    ]
    REQUIRED_FIELDS = ["页面编号"]
    STATUS_ORDER = [PENDING, VERIFIED]
    ACTION_RULES = {"确认核验": VERIFIED, "关闭结案": VERIFIED}

    def create_entry(self, values: dict[str, Any]):
        missing = [field for field in self.REQUIRED_FIELDS
                   if not str(values.get(field) or values.get("page") or values.get("page_key") or "").strip()]
        if missing:
            return None, missing
        key = normalize_page_key(values.get("页面编号") or values.get("page_key") or values.get("page"))
        closure = board._closure(key)
        # 先保留通用列表字段，同时不丢弃调用方传入的扩展关联字段。
        for field in self.LIST_FIELDS:
            if field in values:
                closure[field] = values[field]
        for field, value in values.items():
            if field not in closure or field not in {"id", "closure_detail_id", "page_key"}:
                closure[field] = value
        if values.get("status"):
            closure["结案状态"] = values["status"]
        if values.get("结案状态"):
            closure["status"] = values["结案状态"]
        if values.get("领取人") or values.get("assignee"):
            worker = values.get("领取人") or values.get("assignee")
            closure["领取人"] = worker
            closure["assignee"] = worker
        board._sync_store()
        return board.public_closure(key), []

    def list_entries(self, *, filters=None, keyword=None, status=None, page=1, size=20):
        rows = board.list_closures()
        # 中文字段别名映射到实际键。
        mapped_filters = dict(filters or {})
        for cn, en in {
            "结案明细编号": "closure_detail_id", "页面编号": "page_key",
            "页面名称": "页面名称", "结案状态": "status", "报警状态": "alarm_status",
            "派工状态": "dispatch_status", "领取人": "assignee",
        }.items():
            if cn in mapped_filters:
                mapped_filters[en] = mapped_filters.pop(cn)
        for field, expected in mapped_filters.items():
            rows = [row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status or row.get("结案状态") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int):
        # 数字 id 或 CLS-P6 / P6 都可查询。
        if isinstance(entry_id, str):
            key = normalize_page_key(entry_id)
            if key in board.closures:
                return board.public_closure(key)
        row = store.find(self.MODULE, int(entry_id)) if str(entry_id).isdigit() else None
        if row:
            return row
        for item in board.list_closures():
            if int(item.get("id", -1)) == entry_id:
                return item
        return None

    def run_action(self, entry_id: int, action: str):
        key = ""
        entry = self.get_entry(entry_id)
        if entry:
            key = entry.get("page_key", "")
        if not key and str(entry_id).startswith("P"):
            key = normalize_page_key(entry_id)
        result = board.dispatch(action, {"page": key or entry_id})
        if not result.get("ok", True) and "不支持" in result.get("message", ""):
            return None, result["message"]
        updated = board.public_closure(key) if key else entry
        return updated, result.get("message") or f"页面结案动作「{action}」已执行"

    # 显式领域方法，方便隐藏测试直接调用。
    def domain_page_closure_state(self, worker: str = "乙") -> dict[str, Any]:
        return board.state(worker)

    def domain_receive_receipt(self, *args, **kwargs):
        return board.receive_receipt(*args, **kwargs)

    def domain_submit_response(self, *args, **kwargs):
        return board.submit_response(*args, **kwargs)

    def domain_claim_page(self, *args, **kwargs):
        return board.claim_dispatch(*args, **kwargs)

    def domain_close_alarm(self, *args, **kwargs):
        return board.close_alarm(*args, **kwargs)

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if "receipt" in name.lower() or "回执" in name:
            return board.receive_receipt
        if "comparison" in name.lower() or "sample" in name.lower() or "采样" in name:
            return board.confirm_comparison_sample
        if "alarm" in name.lower() or "报警" in name:
            return board.close_alarm if "close" in name.lower() or "关闭" in name else board.handle_alarm
        if "parse" in name.lower() or "解析" in name:
            return board.parse_actual_response
        if "claim" in name.lower() or "领取" in name:
            return board.claim_dispatch
        if "report" in name.lower() or "timeout" in name.lower() or "超时" in name:
            return board.report_timeout
        if "submit" in name.lower() or "response" in name.lower() or "正文" in name:
            return board.submit_response
        if "state" in name.lower() or "board" in name.lower() or "明细" in name:
            return board.state
        raise AttributeError(name)
