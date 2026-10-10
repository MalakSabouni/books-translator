#!/usr/bin/env python3
"""Run the EPUB-to-Arabic-EPUB workflow with one command."""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import BadZipFile

import epub_to_markdown
import hy_translator
import markdown_to_epub
import translate_markdown


def run_pipeline(
    input_epub: Path,
    output_epub: Path,
    work_dir: Path,
    target_language: str = "Arabic",
    server_url: str = hy_translator.DEFAULT_URL,
    max_chars: int = 6000,
    extract_only: bool = False,
) -> Path:
    """Extract, translate, and rebuild an EPUB; keep Markdown and cache in work_dir."""
    input_epub = input_epub.resolve()
    output_epub = output_epub.resolve()
    work_dir = work_dir.resolve()
    if input_epub == output_epub:
        raise ValueError("Output EPUB must not overwrite the original EPUB.")
    if max_chars < 1:
        raise ValueError("--max-chars must be positive.")
    if not target_language.strip():
        raise ValueError("--target-language must not be empty.")
    if not extract_only and not hy_translator.is_server_up(server_url):
        raise RuntimeError(
            f"Could not reach the Hy-MT server at {server_url}. "
            "Start it in another PowerShell window with:\n"
            "  llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M"
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    print("Step 1/3: Extracting EPUB text in reading order...")
    book_title, documents, titles_by_path, contents = epub_to_markdown.extract_epub(input_epub)
    source_markdown = epub_to_markdown.markdown_for(
        book_title, documents, titles_by_path, contents
    )
    source_md_path = work_dir / f"{input_epub.stem}.md"
    source_md_path.write_text(source_markdown, encoding="utf-8")
    content_documents = sum("nav" not in document.properties for document in documents)
    print(f"  Extracted {content_documents} content documents to {source_md_path}")

    if extract_only:
        print("Extraction-only mode; translation and EPUB building were skipped.")
        return source_md_path

    print("Step 2/3: Translating Markdown sections...")
    translated_markdown = translate_markdown.translate_markdown(
        source_markdown,
        target_language,
        server_url,
        max_chars,
        work_dir / "translation-cache.json",
    )
    translated_md_path = work_dir / f"{input_epub.stem}.{target_language.lower()}.md"
    translated_md_path.write_text(translated_markdown, encoding="utf-8")
    print(f"  Translated Markdown saved to {translated_md_path}")

    print("Step 3/3: Rebuilding RTL EPUB from original layout...")
    image_count, pagebreak_count = markdown_to_epub.rebuild_epub(
        input_epub, translated_md_path, output_epub
    )
    print(f"  Preserved {image_count} image assets and {pagebreak_count} page-break markers.")
    print(f"Completed: {output_epub}")
    return output_epub


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Translate an EPUB to Arabic and rebuild it as an RTL EPUB, "
            "preserving the original EPUB layout and assets."
        )
    )
    parser.add_argument("input", type=Path, help="Source EPUB file")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output EPUB (default: <input-name>.ar.epub beside the source)",
    )
    parser.add_argument(
        "--work-dir", type=Path,
        help="Intermediate Markdown and translation cache (default: .work/<input-name>)",
    )
    parser.add_argument(
        "--server-url", default=hy_translator.DEFAULT_URL,
        help=f"Hy-MT llama-server URL (default: {hy_translator.DEFAULT_URL})",
    )
    parser.add_argument("--target-language", default="Arabic", help="Translation language")
    parser.add_argument(
        "--max-chars", type=int, default=6000,
        help="Approximate maximum source characters per translation group",
    )
    parser.add_argument(
        "--extract-only", action="store_true",
        help="Only extract Markdown; do not require the model server",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print(f"Input EPUB not found: {args.input}", file=sys.stderr)
        return 2
    output_epub = args.output or args.input.with_name(f"{args.input.stem}.ar.epub")
    work_dir = args.work_dir or Path(".work") / args.input.stem
    try:
        run_pipeline(
            args.input,
            output_epub,
            work_dir,
            target_language=args.target_language,
            server_url=args.server_url,
            max_chars=args.max_chars,
            extract_only=args.extract_only,
        )
    except (OSError, BadZipFile, ValueError, RuntimeError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
