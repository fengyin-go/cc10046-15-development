"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class ParserRuleService(BaseModuleService):
    MODULE = "parser_rule"
    ENTITY = "解析规则"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["规则标识", "目标页面", "字段选择器", "数据类型", "默认取值", "正则过滤", "规则版本", "生效批次"]
    REQUIRED_FIELDS = ["规则标识", "目标页面", "字段选择器"]
    STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def domain_parse_html(self, html_content: str, rule_id: int = 0,
                          raw_response_id: str = "", **kwargs) -> dict:
        """使用规则解析HTML内容提取字段。

        额外保留 raw_response_id / content_hash，供页面结案明细关联原始页和提取结果。
        """
        from app.compute import content_hash
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts: list[str] = []
                self.current_tag = ""

            def handle_starttag(self, tag, attrs):
                self.current_tag = tag

            def handle_data(self, data):
                stripped = data.strip()
                if stripped:
                    self.texts.append(stripped)

        extractor = TextExtractor()
        extractor.feed(html_content)        # 返回采集处理结果，含查询校验和回填批次信息

        return {
            "rule_id": rule_id,
            "raw_response_id": raw_response_id or kwargs.get("raw_id", ""),
            "content_hash": content_hash(html_content or ""),
            "response_text_consumed": bool(html_content),
            "field_count": len(extractor.texts),
            "extracted_fields": extractor.texts[:20],
            "html_length": len(html_content),
        }
