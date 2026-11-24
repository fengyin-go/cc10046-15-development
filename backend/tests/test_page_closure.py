"""采集页面独立结案链路的回归测试。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.page_closure import board, parse_actual_response


VALID_PAGE = """
<article class="product-item" data-sku="P6-1">
  <h2 class="product-title">原有效商品页</h2>
  <span class="product-price">¥66.00</span>
</article>
"""
GATEWAY_PAGE = "<html><head><title>502 Bad Gateway</title></head><body>网关错误</body></html>"


def setup_function():
    board.reset(seed="baseline")


def test_receipt_closes_page_once_and_links_records():
    board.report_timeout(pages=[6, 7], error_type="读取超时")
    board.claim_dispatch(page=6, worker="乙")
    result = board.receive_receipt(
        page=6, receipt_id="RCP-6", success=True, text=VALID_PAGE,
    )

    assert result["valid"] is True
    page = board.public_closure("P6")
    assert page["status"] == "已核验"
    assert page["success_count"] == 1
    assert page["retry_exempt"] is True
    assert page["open_alarm_count"] == 0
    assert page["raw_response_id"]
    assert page["extraction_id"]
    assert page["actual_data_count"] == 1

    duplicate = board.receive_receipt(page=6, receipt_id="RCP-6", success=True, text=VALID_PAGE)
    assert duplicate["duplicate"] is True
    assert page["success_count"] == 1
    assert len(board.confirmed_data) == 1


def test_gateway_body_keeps_error_and_alarm_open():
    board.report_timeout(pages=[7], error_type="读取超时")
    result = board.submit_response(page=7, text=GATEWAY_PAGE, status_code=502)

    assert result["valid"] is False
    assert result["closed"] is False
    page = board.public_closure("P7")
    assert page["status"] == "待补采"
    assert page["open_alarm_count"] == 1
    assert board.close_alarm(page=7)["closed"] is False

    timeout_error = next(e for e in board.errors if e["failure_type"] == "读取超时")
    gateway_error = next(e for e in board.errors if e["failure_type"] == "网关错误")
    assert timeout_error["attempt_id"]
    assert gateway_error["attempt_id"] == timeout_error["attempt_id"]
    assert timeout_error["closure_detail_id"] == gateway_error["closure_detail_id"]


def test_comparison_sample_does_not_count_second_time():
    board.report_timeout(pages=[6])
    board.claim_dispatch(page=6, worker="乙")
    board.submit_response(page=6, text=VALID_PAGE)

    first = board.confirm_comparison_sample(page=6, worker="乙", text=VALID_PAGE)
    second = board.confirm_comparison_sample(page=6, worker="乙", text=VALID_PAGE)

    assert first["counted"] is False
    assert second.get("duplicate") is True
    page = board.public_closure("P6")
    assert page["comparison_count"] == 1
    assert page["success_count"] == 1
    assert len(board.confirmed_data) == 1


def test_other_pages_remain_claimable_after_dispatch_restored():
    board.report_timeout(pages=[6, 7])
    board.claim_dispatch(page=6, worker="乙")
    board.submit_response(page=6, text=VALID_PAGE)
    restored = board.restore_dispatch(worker="乙")

    assert "P7" in restored["claimable_pages"]
    assert "P8" in restored["claimable_pages"]
    claimed = board.claim_dispatch(worker="乙")
    assert claimed["ok"] is True
    assert claimed["page"]["page_key"] in {"P7", "P8"}


def test_parser_consumes_actual_text_and_rejects_gateway():
    rule = {
        "item_selector": ".product-item",
        "fields": [
            {"name": "title", "selector": ".product-title"},
            {"name": "price", "selector": ".product-price"},
        ],
    }
    valid = parse_actual_response(VALID_PAGE, rule)
    gateway = parse_actual_response(GATEWAY_PAGE, rule)

    assert valid["valid"] is True
    assert valid["items"][0]["title"] == "原有效商品页"
    assert valid["response_text_consumed"] is True
    assert gateway["valid"] is False
    assert gateway["failure_type"] == "网关错误"
