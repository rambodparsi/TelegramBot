from telegram.format import markdown_to_html, render_chunks, split_markdown
from telegram.parser import parse_update


def _update(**message):
    base = {"message_id": 7, "chat": {"id": 42, "type": "private"}, "from": {"id": 9, "is_bot": False}}
    return {"update_id": 1, "message": {**base, **message}}


def test_parse_text_and_command():
    assert parse_update(_update(text="hi")).kind == "text"
    cmd = parse_update(_update(text="/new@MyBot please"))
    assert (cmd.kind, cmd.command, cmd.text) == ("command", "new", "please")


def test_parse_photo_picks_largest_and_voice():
    photo = parse_update(_update(photo=[{"file_id": "s", "file_size": 10}, {"file_id": "l", "file_size": 99}], caption="c"))
    assert (photo.kind, photo.file_id, photo.text) == ("photo", "l", "c")
    voice = parse_update(_update(voice={"file_id": "v", "mime_type": "audio/ogg", "file_size": 5}))
    assert (voice.kind, voice.file_id) == ("voice", "v")


def test_parse_ignores_non_messages_and_bots():
    assert parse_update({"update_id": 1, "edited_message": {}}) is None
    assert parse_update(_update(text="x", **{"from": {"id": 1, "is_bot": True}})) is None


def test_markdown_to_html_escapes_and_formats():
    out = markdown_to_html("**Bold** & _it_ <tag> `a<b` [x](https://e.com)\n# Head\n- item")
    assert "<b>Bold</b>" in out and "&amp;" in out and "<i>it</i>" in out
    assert "&lt;tag&gt;" in out and "<code>a&lt;b</code>" in out
    assert '<a href="https://e.com">x</a>' in out and "<b>Head</b>" in out and "• item" in out


def test_code_fence_content_is_not_formatted():
    out = markdown_to_html("```python\nx = 2 * 3 * 4\nif a < b: pass\n```")
    assert out == '<pre><code class="language-python">x = 2 * 3 * 4\nif a &lt; b: pass</code></pre>'


def test_farsi_passes_through():
    assert markdown_to_html("**سلام** دنیا") == "<b>سلام</b> دنیا"


def test_split_keeps_chunks_under_limit_and_balances_fences():
    text = "para\n" * 500 + "```\n" + "code line\n" * 600 + "```\nend"
    chunks = split_markdown(text, limit=1000)
    assert all(len(c) <= 1010 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert all(len(c) <= 4096 for c in render_chunks(text))
