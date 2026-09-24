import html
import re


def render_markdown(source: str) -> str:
    lines = html.escape(source or "").replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for line in lines:
        if line.startswith("```"):
            if in_code:
                blocks.append("<pre><code>" + "\n".join(code) + "</code></pre>")
                code.clear()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,3}) (.+)$", line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1))
            blocks.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*] (.+)$", line)
        if bullet:
            flush_paragraph()
            list_items.append(bullet.group(1))
            continue
        flush_list()
        paragraph.append(line.strip())
    if in_code:
        blocks.append("<pre><code>" + "\n".join(code) + "</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(blocks)


def _inline(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if not url.startswith(("http://", "https://")):
            return label
        return f'<a href="{url}" rel="noopener noreferrer">{label}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)
