"""语义化别名模块：页面结案工作台的核心实现位于 page_closure。"""
from app.services.page_closure import (
    PageClosureBoard,
    PageClosureDetailService,
    board,
    parse_actual_response,
)
from app.services.error_retry import ErrorRetryService

RetryClosureService = PageClosureDetailService
RetryClosureBoard = PageClosureBoard
PageClosureService = PageClosureDetailService

__all__ = [
    "PageClosureBoard",
    "PageClosureDetailService",
    "PageClosureService",
    "RetryClosureBoard",
    "RetryClosureService",
    "ErrorRetryService",
    "board",
    "parse_actual_response",
]
