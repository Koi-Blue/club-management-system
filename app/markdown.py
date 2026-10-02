from markdown_it import MarkdownIt


# 原始 HTML 不执行；解析器同时拦截 javascript/vbscript 等危险链接。
_renderer = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def render_markdown(source: str) -> str:
    return _renderer.render(source or "")
