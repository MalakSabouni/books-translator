#!/usr/bin/env python3
"""Translate Markdown book sections to Arabic using an OpenAI-compatible API."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import hy_translator


SPINE_MARKER = re.compile(r"^\s*<!-- Spine document: (.+?) -->\s*$")
CHAPTER_HEADING = re.compile(r"^\s{0,3}#{1,2}\s+.+$")
MARKDOWN_PREFIX = re.compile(
    r"^(?P<prefix>\s*(?:(?:>\s*)|(?:#{1,6}\s+)|(?:[-+*]\s+)|(?:\d+[.)]\s+))+)"
)
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


@dataclass
class Section:
    label: str
    start: int
    end: int
    translate: bool = True


def find_sections(lines: list[str]) -> list[Section]:
    """Find converter spine sections, or fall back to level-one Markdown chapters."""
    markers = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := SPINE_MARKER.match(line))
    ]
    if markers:
        sections: list[Section] = []
        if markers[0][0] > 0:
            sections.append(Section("Book title and contents", 0, markers[0][0]))
        for index, (start, label) in enumerate(markers):
            end = markers[index + 1][0] if index + 1 < len(markers) else len(lines)
            navigation = any("Navigation document listed above" in line for line in lines[start:end])
            sections.append(Section(label, start, end, not navigation))
        return sections

    headings = [
        index for index, line in enumerate(lines)
        if re.match(r"^\s{0,3}#\s+\S", line)
    ]
    first_heading_is_chapter = bool(
        headings and re.search(r"\b(chapter|part)\b", lines[headings[0]], re.I)
    )
    chapter_starts = headings if first_heading_is_chapter else headings[1:]
    if chapter_starts:
        sections = []
        if chapter_starts[0] > 0:
            sections.append(Section("Book title and front matter", 0, chapter_starts[0]))
        for index, start in enumerate(chapter_starts):
            end = chapter_starts[index + 1] if index + 1 < len(chapter_starts) else len(lines)
            sections.append(Section(lines[start].lstrip("# ").strip(), start, end))
        return sections

    fallback_starts = [
        index for index, line in enumerate(lines)
        if CHAPTER_HEADING.match(line) and re.search(r"\b(chapter|part)\b", line, re.I)
    ]
    if fallback_starts:
        sections = []
        if fallback_starts[0] > 0:
            sections.append(Section("Book title and front matter", 0, fallback_starts[0]))
        for index, start in enumerate(fallback_starts):
            end = fallback_starts[index + 1] if index + 1 < len(fallback_starts) else len(lines)
            sections.append(Section(lines[start].lstrip("# ").strip(), start, end))
        return sections

    raise ValueError(
        "No chapter boundaries found. Use Markdown with chapter headings or "
        "'<!-- Spine document: ... -->' markers."
    )


def _translation_items(lines: list[str], start: int, end: int) -> list[tuple[int, str, str]]:
    """Return (line index, Markdown prefix, translatable text) tuples."""
    items: list[tuple[int, str, str]] = []
    fence_char = ""
    fence_length = 0
    for index in range(start, end):
        line = lines[index]
        fence_match = FENCE.match(line)
        if fence_match:
            fence = fence_match.group(1)
            if not fence_char:
                fence_char, fence_length = fence[0], len(fence)
            elif fence[0] == fence_char and len(fence) >= fence_length:
                fence_char, fence_length = "", 0
            continue
        if fence_char or not line.strip() or line.lstrip().startswith("<!--"):
            continue
        if TABLE_SEPARATOR.match(line) or re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            continue
        prefix_match = MARKDOWN_PREFIX.match(line)
        prefix = prefix_match.group("prefix") if prefix_match else ""
        text = line[len(prefix):]
        if text.strip():
            items.append((index, prefix, text))
    if fence_char:
        raise ValueError("Markdown contains an unclosed fenced code block.")
    return items


def _save_cache(path: Path, cache: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def translate_markdown(
    source: str,
    target_language: str,
    base_url: str,
    max_chars: int,
    cache_path: Path,
) -> str:
    lines = source.splitlines()
    sections = find_sections(lines)
    try:
        cache: dict[str, list[str]] = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            raise ValueError("Translation cache must be a JSON object.")
    except FileNotFoundError:
        cache = {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Translation cache is invalid JSON: {exc}") from exc

    for section_number, section in enumerate(sections, start=1):
        if not section.translate:
            print(f"Keeping navigation section: {section.label}")
            continue
        items = _translation_items(lines, section.start, section.end)
        if not items:
            continue
        print(f"Translating section {section_number}/{len(sections)}: {section.label}")
        chunks: list[list[tuple[int, str, str]]] = []
        current: list[tuple[int, str, str]] = []
        current_chars = 0
        for item in items:
            size = len(item[2])
            if current and current_chars + size > max_chars:
                chunks.append(current)
                current, current_chars = [], 0
            current.append(item)
            current_chars += size
        if current:
            chunks.append(current)

        for chunk_number, chunk in enumerate(chunks, start=1):
            texts = [item[2] for item in chunk]
            cache_material = json.dumps(
                {
                    "chapter": section.label,
                    "texts": texts,
                    "language": target_language,
                    "base_url": base_url.rstrip("/"),
                    "max_chars": max_chars,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
            translated = cache.get(cache_key)
            if (
                not isinstance(translated, list)
                or len(translated) != len(chunk)
                or any(
                    not isinstance(text, str) or not text.strip()
                    for text in translated
                )
            ):
                print(f"  Translating text group {chunk_number}/{len(chunks)}")
                translated = [
                    hy_translator.translate(
                        text,
                        target_language,
                        base_url=base_url,
                        max_chars=max_chars,
                    )
                    for text in texts
                ]
                if any(not text.strip() for text in translated):
                    raise RuntimeError("Hy-MT returned an empty translation.")
                cache[cache_key] = translated
                _save_cache(cache_path, cache)
            for (line_index, prefix, _), translated_text in zip(chunk, translated):
                translated_lines = translated_text.splitlines()
                if prefix.startswith(">"):
                    translated_text = "\n".join("> " + line for line in translated_lines)
                elif re.match(r"^\s*(?:[-+*]|\d+[.)])\s", prefix):
                    continuation = " " * len(prefix)
                    translated_text = "\n".join(
                        [translated_lines[0], *(continuation + line for line in translated_lines[1:])]
                    )
                lines[line_index] = prefix + translated_text

    return "\n".join(lines) + ("\n" if source.endswith(("\n", "\r")) else "")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate logical Markdown chapters to Arabic, one chapter at a time."
    )
    parser.add_argument("input", type=Path, help="Source Markdown file")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output file (default: <input-name>.ar.md)",
    )
    parser.add_argument(
        "--base-url",
        default=hy_translator.DEFAULT_URL,
        help=f"Hy-MT server base URL (default: {hy_translator.DEFAULT_URL})",
    )
    parser.add_argument("--target-language", default="Arabic", help="Translation language")
    parser.add_argument(
        "--max-chars", type=int, default=6000,
        help="Approximate maximum source characters per request",
    )
    parser.add_argument(
        "--cache-file", type=Path,
        help="Translation checkpoint (default: <output>.translation-cache.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print(f"Input Markdown not found: {args.input}", file=sys.stderr)
        return 2
    if args.max_chars < 1:
        print("--max-chars must be positive.", file=sys.stderr)
        return 2
    output_path = args.output or args.input.with_name(f"{args.input.stem}.ar.md")
    if args.input.resolve() == output_path.resolve():
        print("Input and output paths must be different.", file=sys.stderr)
        return 2
    cache_path = args.cache_file or output_path.with_suffix(".translation-cache.json")
    try:
        source = args.input.read_text(encoding="utf-8-sig")
        translated = translate_markdown(
            source, args.target_language, args.base_url, args.max_chars, cache_path
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(translated, encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Translated Markdown: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
