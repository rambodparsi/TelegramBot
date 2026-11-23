"""Convert model Markdown to Telegram-safe HTML and split long replies."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
CHUNK_TARGET = 3500  # leave headroom for HTML tags added during conversion

_FENCE = re.compile(r"```[ \t]*([\w+-]*)\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_BULLET = re.compile(r"^(\s*)[*-]\s+", re.M)


def markdown_to_html(text: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def fence(m: re.Match) -> str:
        lang, body = m.group(1), html.escape(m.group(2).rstrip("\n"), quote=False)
        if lang:
            return keep(f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>')
        return keep(f"<pre>{body}</pre>")

    out = _FENCE.sub(fence, text)
    out = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), out)
    out = _LINK.sub(lambda m: keep(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1), quote=False)}</a>'), out)
    out = html.escape(out, quote=False)
    out = _HEADING.sub(lambda m: f"<b>{m.group(1).strip()}</b>", out)
    out = _BULLET.sub(lambda m: f"{m.group(1)}• ", out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", out)
    out = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", out)
    out = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], out)


def split_markdown(text: str, limit: int = CHUNK_TARGET) -> list[str]:
    """Split on paragraph/line boundaries, never inside a ``` code fence when avoidable."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    in_fence = False
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            if in_fence:
                current += "\n```"
            chunks.append(current)
            current = ("```\n" if in_fence else "") + line
        else:
            current = candidate
        if line.strip().startswith("```"):
            in_fence = not in_fence
        while len(current) > limit:  # single enormous line
            chunks.append(current[:limit])
            current = current[limit:]
    if current.strip():
        chunks.append(current)
    return chunks


def render_chunks(markdown: str) -> list[str]:
    return [markdown_to_html(c) for c in split_markdown(markdown)]
