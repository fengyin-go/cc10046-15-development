"""页面结案明细服务的文件命名别名。"""
from app.services.page_closure import (
    PageClosureBoard,
    PageClosureDetailService,
    board,
    parse_actual_response,
)

PageClosureDetail = PageClosureDetailService
PageClosureService = PageClosureDetailService

__all__ = [
    "PageClosureBoard",
    "PageClosureDetailService",
    "PageClosureDetail",
    "PageClosureService",
    "board",
    "parse_actual_response",
]
