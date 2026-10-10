#!/usr/bin/env python3
"""Convert an EPUB's spine and navigation titles to a Markdown file."""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

BLOCK_TAGS = {
    "address", "blockquote", "caption", "dd", "dt", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre",
}
TABLE_CELL_TAGS = {"td", "th"}


@dataclass
class SpineDocument:
    path: str
    media_type: str
    properties: set[str]
    root: ET.Element


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def normalized_text(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def resolve_href(base_path: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#", 1)[0].split("?", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), href))


def _has_block_descendant(element: ET.Element) -> bool:
    return any(local_name(node.tag) in BLOCK_TAGS or local_name(node.tag) == "table"
               for node in element.iter() if node is not element)


def _render_table(element: ET.Element) -> list[str]:
    rows: list[list[str]] = []
    has_header = False
    for row in element.iter():
        if local_name(row.tag) != "tr":
            continue
        cells = [cell for cell in list(row) if local_name(cell.tag) in TABLE_CELL_TAGS]
        if not cells:
            continue
        has_header = has_header or any(local_name(cell.tag) == "th" for cell in cells)
        rows.append([normalized_text(cell).replace("|", r"\|") for cell in cells])
    if not rows:
        return []

    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    rendered = ["| " + " | ".join(rows[0]) + " |"]
    if has_header:
        rendered.append("| " + " | ".join("---" for _ in range(width)) + " |")
    rendered.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    captions = [
        f"**{normalized_text(node)}**"
        for node in element.iter()
        if local_name(node.tag) == "caption" and normalized_text(node)
    ]
    return captions + ["\n".join(rendered)]


def _render_block(element: ET.Element) -> str:
    tag = local_name(element.tag)
    if tag == "pre":
        code = "".join(element.itertext()).strip("\n")
        if not code.strip():
            return ""
        longest_run = max((len(run) for run in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest_run + 1)
        return f"{fence}\n{code}\n{fence}"

    text = normalized_text(element)
    if not text:
        return ""
    if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
        return f"{'#' * int(tag[1])} {text}"
    if tag == "li":
        return f"- {text}"
    if tag == "blockquote":
        return "\n".join(f"> {line}" for line in text.splitlines())
    if tag in {"dt", "caption", "figcaption"}:
        return f"**{text}**"
    return text


def render_body(body: ET.Element) -> list[str]:
    rendered: list[str] = []

    def walk(element: ET.Element) -> None:
        tag = local_name(element.tag)
        if tag == "table":
            rendered.extend(_render_table(element))
            return
        if tag in BLOCK_TAGS:
            block = _render_block(element)
            if block:
                rendered.append(block)
            return
        if not _has_block_descendant(element):
            text = normalized_text(element)
            if text:
                rendered.append(text)
            return
        for child in list(element):
            walk(child)

    for child in list(body):
        walk(child)
    return rendered


def _navigation_titles(
    archive: ZipFile, manifest: dict[str, tuple[str, str, set[str]]], opf_path: str
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    titles_by_path: dict[str, list[str]] = {}
    contents: list[tuple[str, str]] = []
    seen_contents: set[str] = set()

    def add_title(path: str, title: str) -> None:
        title = " ".join(title.split())
        if not title:
            return
        labels = titles_by_path.setdefault(path, [])
        if title.casefold() not in {label.casefold() for label in labels}:
            labels.append(title)
        if path not in seen_contents:
            contents.append((path, title))
            seen_contents.add(path)

    for path, media_type, properties in manifest.values():
        if "nav" in properties:
            root = ET.fromstring(archive.read(path))
            for anchor in root.iter():
                if local_name(anchor.tag) == "a" and anchor.get("href"):
                    add_title(resolve_href(path, anchor.attrib["href"]), normalized_text(anchor))
        elif media_type == "application/x-dtbncx+xml":
            root = ET.fromstring(archive.read(path))
            for point in root.iter():
                if local_name(point.tag) != "navpoint":
                    continue
                label = next(
                    (normalized_text(node) for node in point.iter()
                     if local_name(node.tag) == "text" and normalized_text(node)),
                    "",
                )
                content = next(
                    (node.get("src") for node in point.iter()
                     if local_name(node.tag) == "content" and node.get("src")),
                    None,
                )
                if content and label:
                    add_title(resolve_href(path, content), label)
    return titles_by_path, contents


def extract_epub(epub_path: Path) -> tuple[str, list[SpineDocument], dict[str, list[str]], list[tuple[str, str]]]:
    with ZipFile(epub_path) as archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except KeyError as exc:
            raise ValueError("EPUB is missing META-INF/container.xml.") from exc
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("EPUB container does not identify its package document.")

        opf_path = urllib.parse.unquote(rootfile.attrib["full-path"])
        opf = ET.fromstring(archive.read(opf_path))
        metadata = opf.find(f"{{{OPF_NS}}}metadata")
        title_node = metadata.find(f"{{{DC_NS}}}title") if metadata is not None else None
        book_title = normalized_text(title_node) if title_node is not None else ""

        manifest: dict[str, tuple[str, str, set[str]]] = {}
        for item in opf.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item"):
            item_id, href = item.get("id"), item.get("href")
            if item_id and href:
                path = resolve_href(opf_path, href)
                manifest[item_id] = (
                    path,
                    item.get("media-type", ""),
                    set(item.get("properties", "").split()),
                )

        titles_by_path, contents = _navigation_titles(archive, manifest, opf_path)
        spine = opf.find(f"{{{OPF_NS}}}spine")
        if spine is None:
            raise ValueError("EPUB package document has no spine.")

        documents: list[SpineDocument] = []
        itemrefs = spine.findall(f"{{{OPF_NS}}}itemref")
        if not itemrefs:
            raise ValueError("EPUB spine contains no reading-order documents.")
        for itemref in itemrefs:
            item_id = itemref.get("idref", "")
            if item_id not in manifest:
                raise ValueError(f"EPUB spine references missing manifest item {item_id!r}.")
            path, media_type, properties = manifest[item_id]
            if media_type not in {"application/xhtml+xml", "text/html"}:
                raise ValueError(f"Unsupported spine document type {media_type!r}: {path}")
            try:
                root = ET.fromstring(archive.read(path))
            except (KeyError, ET.ParseError) as exc:
                raise ValueError(f"Could not read EPUB spine document {path}: {exc}") from exc
            documents.append(SpineDocument(path, media_type, properties, root))

    if not book_title:
        book_title = next(
            (normalized_text(node) for document in documents for node in document.root.iter()
             if local_name(node.tag) == "title" and normalized_text(node)),
            epub_path.stem,
        )
    return book_title, documents, titles_by_path, contents


def markdown_for(
    book_title: str,
    documents: list[SpineDocument],
    titles_by_path: dict[str, list[str]],
    contents: list[tuple[str, str]],
) -> str:
    sections = [f"# {book_title}"]
    if contents:
        sections.extend(["", "## Contents", ""])
        sections.extend(f"- {title}" for _, title in contents)

    for document in documents:
        sections.extend(["", f"<!-- Spine document: {document.path} -->"])
        if "nav" in document.properties:
            sections.append("<!-- Navigation document listed above. -->")
            continue

        body = next(
            (node for node in document.root.iter() if local_name(node.tag) == "body"),
            document.root,
        )
        blocks = render_body(body)
        document_title = next(
            (normalized_text(node) for node in document.root.iter()
             if local_name(node.tag) == "title" and normalized_text(node)),
            "",
        )
        titles = titles_by_path.get(document.path, [])
        if document_title and document_title.casefold() not in {t.casefold() for t in titles}:
            titles = [*titles, document_title]
        visible_text = {block.casefold() for block in blocks}
        document_sections: list[str] = []
        for title in titles:
            if title.casefold() != book_title.casefold() and title.casefold() not in visible_text:
                document_sections.append(f"## {title}")
                visible_text.add(title.casefold())
        document_sections.extend(blocks)
        if document_sections:
            sections.extend(["", "\n\n".join(document_sections)])
    return "\n".join(sections).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert every EPUB spine document and its navigation titles to Markdown."
    )
    parser.add_argument("input", type=Path, help="Source EPUB file")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output Markdown file (default: same path as input with a .md extension)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print(f"Input EPUB not found: {args.input}", file=sys.stderr)
        return 2
    output_path = args.output or args.input.with_suffix(".md")
    try:
        book_title, documents, titles_by_path, contents = extract_epub(args.input)
        markdown = markdown_for(book_title, documents, titles_by_path, contents)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
    except (OSError, BadZipFile, ValueError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    chapter_count = sum("nav" not in document.properties for document in documents)
    print(f"Converted {len(documents)} spine documents ({chapter_count} content documents).")
    print(f"Book title: {book_title}")
    print(f"Markdown: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
