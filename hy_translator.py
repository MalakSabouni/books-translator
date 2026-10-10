"""
hy_translator.py - reusable wrapper around a local llama-server running Hy-MT2.

Start the server first (in another terminal):
    llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M

Use from other scripts:
    from hy_translator import translate
    print(translate("Hello world", "Arabic"))

Use from the command line:
    python hy_translator.py "Hello world" -l Arabic
    python hy_translator.py -f book.txt -l Arabic -o book_ar.txt
    echo "Hello" | python hy_translator.py -l French

No third-party dependencies (standard library only).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional

# Server location can be overridden with an environment variable.
DEFAULT_URL = os.environ.get("HY_MT_URL", "http://localhost:8080/v1")

# Sampling parameters recommended on the Hy-MT2 model card.
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 0.6,
    "top_k": 20,
    "repeat_penalty": 1.05,
    "max_tokens": 4096,
}

PROMPT_TEMPLATE = (
    "Translate the following text into {language}. "
    "Note that you should only output the translated result "
    "without any additional explanation:\n\n{text}"
)


class TranslatorError(RuntimeError):
    """Raised when the translation server is unreachable or returns an error."""


def _post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer none"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise TranslatorError(
            f"Could not reach llama-server at {url}. Is it running? "
            f"Start it with: llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M\n({e})"
        ) from e


def _translate_one(
    text: str,
    language: str,
    base_url: str,
    timeout: float,
    params: dict,
) -> str:
    payload = {
        "model": "hy-mt2",  # ignored by llama-server
        # No system prompt: the model card says it has no default one.
        "messages": [
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(language=language, text=text),
            }
        ],
        **params,
    }
    data = _post(f"{base_url.rstrip('/')}/chat/completions", payload, timeout)
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise TranslatorError(f"Unexpected server response: {data}") from e


def split_text(text: str, max_chars: int = 1500) -> list[str]:
    """Split text into chunks on paragraph, then line, then hard boundaries."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        if len(para) > max_chars:
            # Oversized paragraph: break by lines, then by characters.
            pieces = []
            for line in para.split("\n"):
                while len(line) > max_chars:
                    pieces.append(line[:max_chars])
                    line = line[max_chars:]
                pieces.append(line)
            sub = "\n".join(pieces)
            if current:
                chunks.append(current)
                current = ""
            # Re-pack the pieces under the limit.
            buf = ""
            for piece in pieces:
                if buf and len(buf) + len(piece) + 1 > max_chars:
                    chunks.append(buf)
                    buf = piece
                else:
                    buf = f"{buf}\n{piece}" if buf else piece
            if buf:
                chunks.append(buf)
            continue
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def translate(
    text: str,
    target_language: str = "Arabic",
    *,
    base_url: str = DEFAULT_URL,
    timeout: float = 300.0,
    max_chars: int = 1500,
    on_progress: Optional[Callable[[int, int], None]] = None,
    **overrides,
) -> str:
    """
    Translate `text` into `target_language` (use the full language name,
    e.g. "Arabic", "French", "Simplified Chinese").

    Long text is split into chunks (about `max_chars` each) and the results are
    joined back together with blank lines. Extra keyword arguments such as
    temperature=0.3 override the default sampling parameters.
    """
    if not text or not text.strip():
        return ""

    params = {**DEFAULT_PARAMS, **overrides}
    chunks = split_text(text, max_chars=max_chars)
    results = []
    for i, chunk in enumerate(chunks, 1):
        results.append(_translate_one(chunk, target_language, base_url, timeout, params))
        if on_progress:
            on_progress(i, len(chunks))
    return "\n\n".join(results)


def translate_many(
    texts: Iterable[str],
    target_language: str = "Arabic",
    **kwargs,
) -> list[str]:
    """Translate a list of strings, returning a list in the same order."""
    return [translate(t, target_language, **kwargs) for t in texts]


def is_server_up(base_url: str = DEFAULT_URL) -> bool:
    """Quick check that llama-server is reachable."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        with urllib.request.urlopen(f"{root}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _main() -> int:
    ap = argparse.ArgumentParser(description="Translate text with a local Hy-MT2 server.")
    ap.add_argument("text", nargs="?", help="Text to translate (or use -f / stdin).")
    ap.add_argument("-l", "--language", default="Arabic", help="Target language (default: Arabic).")
    ap.add_argument("-f", "--file", help="Read input text from this file (UTF-8).")
    ap.add_argument("-o", "--output", help="Write the translation to this file (UTF-8).")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Server base URL (default: {DEFAULT_URL}).")
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.error("provide text, --file, or pipe text via stdin")

    def progress(done: int, total: int) -> None:
        if total > 1:
            print(f"Translated {done}/{total} chunks", file=sys.stderr, end="\r")

    try:
        result = translate(text, args.language, base_url=args.url, on_progress=progress)
    except TranslatorError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(result)
        print(f"\nSaved to {args.output}", file=sys.stderr)
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # avoid Windows console encoding errors
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_main())