"""Voice -> text with the transcription model chain."""

from __future__ import annotations

from llm.gemini import GeminiClient, GeminiError
from media.download import Attachment

PROMPT = (
    "Transcribe this audio exactly, in the language(s) spoken (it is usually English or Farsi). "
    "Output only the transcript text, with no commentary, labels, or timestamps. "
    "If there is no intelligible speech, output nothing."
)


async def transcribe(llm: GeminiClient, models: list[str], audio: Attachment) -> tuple[str, dict]:
    """Try each model until one returns a non-empty transcript. Returns (text, usage)."""
    contents = [{"role": "user", "parts": [audio.gemini_part(), {"text": PROMPT}]}]
    last_usage: dict = {"role": "transcribe", "model": None, "fallbacks": 0}
    for index, model in enumerate(models):
        try:
            result = await llm.generate([model], contents, max_output_tokens=4096, thinking_level=None,
                                        temperature=0)
        except GeminiError as exc:
            if exc.status in (400,) and index + 1 < len(models):
                continue  # e.g. model doesn't accept this audio format
            if exc.retryable and index + 1 < len(models):
                continue
            raise
        last_usage = {"role": "transcribe", "model": result.model, "input_tokens": result.input_tokens,
                      "output_tokens": result.output_tokens, "fallbacks": index}
        if result.text.strip():
            return result.text.strip(), last_usage
    return "", last_usage
