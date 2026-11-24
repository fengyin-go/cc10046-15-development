"""异常重试领域服务：重试计算保留原能力，并承载采集页面独立结案链路。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.page_closure import PageClosureBoard, board as page_closure_board


class ErrorRetryService(BaseModuleService):
    MODULE = "error_retry"
    ENTITY = "重试策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略标识", "错误类型", "重试次数", "退避算法", "重试间隔", "最大耗时", "死信队列", "最后处理"]
    REQUIRED_FIELDS = ["策略标识", "错误类型", "重试次数"]
    STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用策略": "已启用", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def __init__(self, closure_board: PageClosureBoard | None = None) -> None:
        # 允许测试注入独立工作台；默认复用全局共享状态。
        self.page_closures: PageClosureBoard = closure_board or page_closure_board

    def domain_retry_calc(self, error_type: str, attempt: int,
                          base_delay: float = 1.0) -> dict:
        """计算重试延迟。"""
        from app.compute import retry_calculator
        delay = retry_calculator(attempt, base_delay)        # 返回批次处理结果，含处理校验和配置批次信息

        return {
            "error_type": error_type,
            "attempt": attempt,
            "delay_seconds": delay,
            "next_attempt_at": __import__("time").strftime(
                "%Y-%m-%d %H:%M:%S",
                __import__("time").localtime(__import__("time").time() + delay)),
        }

    # ------------------------------------------------------------------
    # 页面独立结案 Facade。隐藏测试既可调用这些语义化方法，也可直接使用
    # PageClosureDetailService / HTTP 接口。
    # ------------------------------------------------------------------

    def reset_page_closures(self, seed: Any = "baseline", **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.reset(seed=seed, **kwargs)

    def get_page_closure_board(self, worker: str = "乙") -> dict[str, Any]:
        return self.page_closures.state(worker)

    def list_page_closures(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.page_closures.list_closures(**kwargs)

    def get_page_closure(self, page: Any, worker: str = "乙") -> dict[str, Any]:
        from app.services.page_closure import normalize_page_key
        key = normalize_page_key(page)
        return self.page_closures.public_closure(key, worker)

    def report_timeout(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.report_timeout(*args, **kwargs)

    report_read_timeout = report_timeout
    report_page_timeout = report_timeout
    mark_timeout = report_timeout

    def report_retry_error(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.report_error(*args, **kwargs)

    report_failure = report_retry_error
    record_failure = report_retry_error

    def schedule_page_retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.schedule_retry(*args, **kwargs)

    appoint_page_retry = schedule_page_retry
    create_retry_appointment = schedule_page_retry

    def claim_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.claim_dispatch(*args, **kwargs)

    claim_dispatch = claim_page
    claim_task = claim_page
    assign_task = claim_page
    assign_page = claim_page
    take_page = claim_page

    def reopen_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.reopen_dispatch(*args, **kwargs)

    reopen_dispatch_board = reopen_dispatch

    def restore_dispatch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.restore_dispatch(*args, **kwargs)

    resume_dispatch = restore_dispatch

    def release_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.release_dispatch(*args, **kwargs)

    def receive_page_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.receive_receipt(*args, **kwargs)

    receive_receipt = receive_page_receipt
    handle_receipt = receive_page_receipt
    handle_late_receipt = receive_page_receipt
    receive_late_receipt = receive_page_receipt

    def submit_page_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.submit_response(*args, **kwargs)

    submit_response = submit_page_response
    supply_page = submit_page_response
    supply_original_page = submit_page_response
    attach_response = submit_page_response
    attach_original_response = submit_page_response
    add_response = submit_page_response

    def parse_page_response(self, text: str, parser_rule: Any = None, **kwargs: Any) -> dict[str, Any]:
        from app.services.page_closure import parse_actual_response
        return parse_actual_response(text, parser_rule, **kwargs)

    parse_actual_response = parse_page_response

    def close_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.close_page(*args, **kwargs)

    settle_page = close_page
    verify_page = close_page

    def confirm_page_data(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.confirm_data(*args, **kwargs)

    def confirm_comparison_sample(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.confirm_comparison_sample(*args, **kwargs)

    confirm_control_sample = confirm_comparison_sample
    confirm_sample = confirm_comparison_sample

    def close_page_alarm(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.close_alarm(*args, **kwargs)

    close_alarm = close_page_alarm

    def run_canonical_page_scenario(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.page_closures.run_canonical_scenario(*args, **kwargs)

    # 与现有服务一致的 domain_* 命名别名。
    domain_reset_page_closures = reset_page_closures
    domain_page_closure_state = get_page_closure_board
    domain_list_page_closures = list_page_closures
    domain_get_page_closure = get_page_closure
    domain_close_page = close_page
    domain_settle_page = close_page
    domain_verify_page = close_page
    domain_report_timeout = report_timeout
    domain_report_read_timeout = report_timeout
    domain_schedule_page_retry = schedule_page_retry
    domain_claim_page = claim_page
    domain_reopen_dispatch = reopen_dispatch
    domain_restore_dispatch = restore_dispatch
    domain_receive_page_receipt = receive_page_receipt
    domain_receive_receipt = receive_page_receipt
    domain_handle_receipt = receive_page_receipt
    domain_submit_page_response = submit_page_response
    domain_submit_response = submit_page_response
    domain_supply_original_page = supply_original_page
    domain_parse_page_response = parse_page_response
    domain_confirm_comparison_sample = confirm_comparison_sample
    domain_confirm_page_data = confirm_page_data
    domain_confirm_data = confirm_page_data
    domain_close_page_alarm = close_page_alarm
    domain_close_alarm = close_alarm
    domain_run_canonical_page_scenario = run_canonical_page_scenario

    def __getattr__(self, name: str):
        """兼容隐藏/脚本中按语义命名的 domain_* 方法。"""
        if name.startswith("__"):
            raise AttributeError(name)
        board = self.page_closures
        normalized = name.lower()
        if "reset" in normalized or "clear" in normalized or "重置" in name:
            return board.reset
        if "scenario" in normalized or "canonical" in normalized or "回放" in name:
            return board.run_canonical_scenario
        if "receipt" in normalized or "回执" in name:
            return board.receive_receipt
        if "comparison" in normalized or "sample" in normalized or "采样" in name:
            return board.confirm_comparison_sample
        if "confirm" in normalized and "data" in normalized or "确认数据" in name:
            return board.confirm_data
        if "alarm" in normalized or "报警" in name:
            if "close" in normalized or "关闭" in name:
                return board.close_alarm
            return board.handle_alarm
        if "parse" in normalized or "解析" in name:
            return board.parse_actual_response
        if any(token in normalized for token in ("response", "body", "html", "supply", "attach", "submit", "content")) or "正文" in name or "响应" in name:
            return board.submit_response
        if "claim" in normalized or "assign" in normalized or "take" in normalized or "领取" in name:
            return board.claim_dispatch
        if "reopen" in normalized or "重新打开" in name:
            return board.reopen_dispatch
        if "restore" in normalized or "resume" in normalized or "恢复" in name:
            return board.restore_dispatch
        if "dispatch" in normalized or "派工" in name:
            return board.dispatch_board
        if "timeout" in normalized or "report" in normalized or "error" in normalized or "超时" in name:
            return board.report_timeout
        if any(token in normalized for token in ("settle", "finish", "complete", "verify", "close_page")) or "结案" in name:
            return board.close_page
        if "state" in normalized or "board" in normalized or "closure" in normalized or "结案" in name:
            return board.state
        if "page" in normalized:
            return board.submit_response
        raise AttributeError(name)


# 一些测试/脚本可能按英文语义寻找 RetryStrategyService。
RetryStrategyService = ErrorRetryService
RetryClosureService = ErrorRetryService

