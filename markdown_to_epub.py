#!/usr/bin/env python3
"""Rebuild an Arabic EPUB from translated Markdown and its original EPUB."""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZIP_STORED, ZipFile


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

ET.register_namespace("", XHTML_NS)
ET.register_namespace("epub", EPUB_NS)
ET.register_namespace("opf", OPF_NS)
ET.register_namespace("dc", DC_NS)

BLOCK_TAGS = {
    "address", "blockquote", "caption", "dd", "dt", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre",
}
SPINE_MARKER = re.compile(r"^\s*<!-- Spine document: (.+?) -->\s*$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


@dataclass
class Unit:
    kind: str
    element: ET.Element


@dataclass
class SpineItem:
    path: str
    media_type: str
    properties: set[str]
    root: ET.Element


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_xml(data: bytes) -> ET.Element:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.fromstring(data, parser=parser)


def text_content(element: ET.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def normalized_path(base: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#", 1)[0].split("?", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), href))


def read_package(archive: ZipFile) -> tuple[str, ET.Element, dict[str, tuple[str, str, set[str]]], list[SpineItem]]:
    try:
        container = parse_xml(archive.read("META-INF/container.xml"))
    except KeyError as exc:
        raise ValueError("Original EPUB is missing META-INF/container.xml.") from exc
    rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container does not identify its package document.")

    opf_path = urllib.parse.unquote(rootfile.attrib["full-path"])
    opf = parse_xml(archive.read(opf_path))
    manifest: dict[str, tuple[str, str, set[str]]] = {}
    for item in opf.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item"):
        item_id, href = item.get("id"), item.get("href")
        if item_id and href:
            manifest[item_id] = (
                normalized_path(opf_path, href),
                item.get("media-type", ""),
                set(item.get("properties", "").split()),
            )

    spine = opf.find(f"{{{OPF_NS}}}spine")
    if spine is None:
        raise ValueError("EPUB package document has no spine.")
    spine_items: list[SpineItem] = []
    for itemref in spine.findall(f"{{{OPF_NS}}}itemref"):
        item_id = itemref.get("idref", "")
        if item_id not in manifest:
            raise ValueError(f"EPUB spine references missing manifest item {item_id!r}.")
        path, media_type, properties = manifest[item_id]
        if media_type not in {"application/xhtml+xml", "text/html"}:
            raise ValueError(f"Unsupported spine document type {media_type!r}: {path}")
        try:
            root = parse_xml(archive.read(path))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError(f"Could not parse EPUB document {path}: {exc}") from exc
        spine_items.append(SpineItem(path, media_type, properties, root))
    if not spine_items:
        raise ValueError("Original EPUB spine contains no documents.")
    return opf_path, opf, manifest, spine_items


def _has_block_descendant(element: ET.Element) -> bool:
    return any(
        local_name(node.tag) in BLOCK_TAGS or local_name(node.tag) == "table"
        for node in element.iter()
        if node is not element
    )


def collect_units(body: ET.Element) -> list[Unit]:
    units: list[Unit] = []

    def walk(element: ET.Element) -> None:
        tag = local_name(element.tag)
        if tag == "table":
            for node in element.iter():
                if local_name(node.tag) == "caption" and text_content(node):
                    units.append(Unit("caption", node))
            if any(local_name(node.tag) == "tr" for node in element.iter()):
                units.append(Unit("table", element))
            return
        if tag in BLOCK_TAGS:
            if text_content(element):
                if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                    kind = f"heading-{tag[1]}"
                elif tag == "li":
                    kind = "list"
                elif tag == "blockquote":
                    kind = "quote"
                elif tag == "pre":
                    kind = "code"
                elif tag in {"caption", "figcaption", "dt"}:
                    kind = "caption"
                else:
                    kind = "paragraph"
                units.append(Unit(kind, element))
            return
        if not _has_block_descendant(element):
            if text_content(element):
                units.append(Unit("paragraph", element))
            return
        for child in list(element):
            walk(child)

    for child in list(body):
        walk(child)
    return units


def _markdown_kind(block: str) -> str:
    stripped = block.strip()
    heading = HEADING.match(stripped)
    if heading:
        return f"heading-{len(heading.group(1))}"
    if FENCE.match(stripped):
        return "code"
    if TABLE_SEPARATOR.match(stripped) or (
        "\n" in stripped and all("|" in line for line in stripped.splitlines())
    ):
        return "table"
    if stripped.startswith(">"):
        return "quote"
    if LIST_ITEM.match(stripped):
        return "list"
    if stripped.startswith("**") and stripped.endswith("**"):
        return "caption"
    return "paragraph"


def split_markdown_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence_char = ""
    fence_size = 0
    for line in lines:
        fence_match = FENCE.match(line)
        if fence_match:
            fence = fence_match.group(1)
            if not fence_char:
                fence_char, fence_size = fence[0], len(fence)
            elif fence[0] == fence_char and len(fence) >= fence_size:
                fence_char, fence_size = "", 0
            current.append(line)
            continue
        if not fence_char and not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if line.lstrip().startswith("<!--"):
            continue
        current.append(line)
    if fence_char:
        raise ValueError("Translated Markdown contains an unclosed code fence.")
    if current:
        blocks.append("\n".join(current))
    return blocks


def translated_sections(markdown: str) -> tuple[str, list[str], dict[str, list[str]]]:
    lines = markdown.lstrip("\ufeff").splitlines()
    markers = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := SPINE_MARKER.match(line))
    ]
    if not markers:
        raise ValueError(
            "Translated Markdown must come from epub_to_markdown.py and contain "
            "'<!-- Spine document: ... -->' markers."
        )
    sections: dict[str, list[str]] = {}
    for index, (start, path) in enumerate(markers):
        end = markers[index + 1][0] if index + 1 < len(markers) else len(lines)
        if path in sections:
            raise ValueError(f"Translated Markdown contains duplicate spine marker {path!r}.")
        sections[path] = lines[start + 1:end]

    title_match = next((HEADING.match(line) for line in lines[:markers[0][0]]
                        if HEADING.match(line)), None)
    book_title = title_match.group(2).strip() if title_match else ""
    toc_lines: list[str] = []
    before_spine = lines[:markers[0][0]]
    contents_index = next(
        (index for index, line in enumerate(before_spine)
         if re.match(r"^\s{0,3}##\s+\S", line)),
        None,
    )
    if contents_index is not None:
        collecting = False
        for line in before_spine[contents_index + 1:]:
            if LIST_ITEM.match(line):
                toc_lines.append(LIST_ITEM.sub("", line, count=1).strip())
                collecting = True
            elif collecting and not line.strip():
                break
            elif collecting:
                break
    return book_title, toc_lines, sections


def _strip_markdown(text: str, *, preserve_list_marker: bool = False) -> str:
    text = text.strip()
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    if not preserve_list_marker:
        text = re.sub(r"^\s*(?:[-+*]|\d+[.)])\s+", "", text)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*`{3,}.*?$|^\s*~{3,}.*?$", "", text, flags=re.M)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(`+|\*\*|__|(?<!\*)\*(?!\*)|(?<!_)_(?!_))", "", text)
    return " ".join(text.split())


def _table_cells(row: str) -> list[str]:
    row = row.strip().strip("|")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in row:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    cells.append("".join(current).strip())
    return cells


def _preserve_replace(element: ET.Element, text: str) -> None:
    """Replace textual content while retaining inline elements and page markers."""
    slots: list[tuple[ET.Element, str]] = []

    def walk(node: ET.Element, inside_pagebreak: bool = False) -> None:
        pagebreak = inside_pagebreak or _is_pagebreak(node)
        if pagebreak:
            return
        if node.text:
            slots.append((node, "text"))
        for child in list(node):
            walk(child, pagebreak)
            if child.tail:
                slots.append((child, "tail"))

    walk(element)
    if not slots:
        element.text = text
        return
    first_node, first_attribute = slots[0]
    setattr(first_node, first_attribute, text)
    for node, attribute in slots[1:]:
        setattr(node, attribute, "")


def _is_pagebreak(node: ET.Element) -> bool:
    return (
        node.get(f"{{{EPUB_NS}}}type", "").lower() == "pagebreak"
        or "pagebreak" in node.get("class", "").lower()
        or "pagebreak" in node.get("role", "").lower()
    )


def _apply_table(unit: Unit, translated: str) -> None:
    rows = [
        _table_cells(line)
        for line in translated.splitlines()
        if "|" in line and not TABLE_SEPARATOR.match(line)
    ]
    source_rows = [
        row for row in unit.element.iter()
        if local_name(row.tag) == "tr"
    ]
    if len(rows) != len(source_rows):
        raise ValueError(
            f"Translated table has {len(rows)} rows; original table has {len(source_rows)}."
        )
    for row_index, source_row in enumerate(source_rows):
        cells = [cell for cell in list(source_row) if local_name(cell.tag) in {"td", "th"}]
        if len(rows[row_index]) != len(cells):
            raise ValueError(
                f"Translated table row {row_index + 1} has {len(rows[row_index])} "
                f"cells; original row has {len(cells)}."
            )
        for cell, translated_cell in zip(cells, rows[row_index]):
            _preserve_replace(cell, _strip_markdown(translated_cell))


def _set_inline_rtl(element: ET.Element) -> None:
    element.set("dir", "rtl")
    existing = element.get("style", "").strip().rstrip(";")
    rtl_style = "direction: rtl !important; text-align: right !important"
    element.set("style", f"{existing}; {rtl_style}" if existing else rtl_style)


def _compatible_block(source_kind: str, markdown_kind: str) -> bool:
    if source_kind.startswith("heading-"):
        return source_kind == markdown_kind
    if source_kind == "paragraph":
        return markdown_kind in {"paragraph", "list", "caption"}
    if source_kind == "list":
        return markdown_kind in {"list", "paragraph"}
    if source_kind == "quote":
        return markdown_kind in {"quote", "paragraph"}
    if source_kind == "caption":
        return markdown_kind in {"caption", "paragraph"}
    return source_kind == markdown_kind


def _align_blocks(document: SpineItem, units: list[Unit], blocks: list[str]) -> list[str]:
    """Drop only generated document-title headings and align content in order."""
    source_kinds = [unit.kind for unit in units]
    markdown_kinds = [_markdown_kind(block) for block in blocks]
    surplus = len(blocks) - len(units)
    if surplus > 0:
        removable = 0
        for kind in markdown_kinds:
            if kind != "heading-2" or removable == surplus:
                break
            removable += 1
        if removable != surplus:
            raise ValueError(
                f"Cannot map {document.path}: Markdown has {surplus} more blocks than "
                "the original, but they are not all generated leading level-2 titles."
            )
        blocks = blocks[surplus:]
        markdown_kinds = markdown_kinds[surplus:]
    elif surplus < 0:
        raise ValueError(
            f"Cannot map {document.path}: Markdown has {abs(surplus)} fewer content "
            "blocks than the original EPUB."
        )

    if len(blocks) != len(units):
        raise ValueError(
            f"Cannot map {document.path}: expected {len(units)} translated blocks, "
            f"found {len(blocks)} after removing generated titles."
        )
    for index, (source_kind, markdown_kind) in enumerate(zip(source_kinds, markdown_kinds), start=1):
        if not _compatible_block(source_kind, markdown_kind):
            raise ValueError(
                f"Cannot map block {index} in {document.path}: original block type "
                f"{source_kind!r} does not match Markdown block type {markdown_kind!r}."
            )
    return blocks


def update_document(document: SpineItem, blocks: list[str], book_title: str) -> None:
    root = document.root
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), root)
    units = collect_units(body)
    blocks = _align_blocks(document, units, blocks)
    for unit, translated in zip(units, blocks):
        if unit.kind == "table":
            _apply_table(unit, translated)
            for node in unit.element.iter():
                if local_name(node.tag) in {"table", "td", "th"}:
                    _set_inline_rtl(node)
        else:
            value = _strip_markdown(
                translated,
                preserve_list_marker=unit.kind == "paragraph"
                and _markdown_kind(translated) == "list",
            )
            if not value:
                raise ValueError(f"Empty translated text block in {document.path}.")
            _preserve_replace(unit.element, value)
            _set_inline_rtl(unit.element)

    first_heading = next((unit.element for unit in units if unit.kind.startswith("heading-")), None)
    document_title = text_content(first_heading) if first_heading is not None else book_title
    for node in root.iter():
        name = local_name(node.tag)
        if name == "title" and document_title:
            node.text = document_title
    root.set("lang", "ar")
    root.set(f"{{{XML_NS}}}lang", "ar")
    root.set("dir", "rtl")
    body.set("dir", "rtl")
    _set_inline_rtl(body)
    _set_rtl_styles(root)


def _set_rtl_styles(root: ET.Element) -> None:
    head = next((node for node in root.iter() if local_name(node.tag) == "head"), None)
    if head is None:
        return
    css = (
        "html, body { direction: rtl !important; } "
        "body { text-align: right !important; } "
        "body p, body h1, body h2, body h3, body h4, body h5, body h6, "
        "body li, body blockquote, body figcaption { "
        "direction: rtl !important; text-align: right !important; }"
    )
    for style in head:
        if local_name(style.tag) == "style" and style.get("id") == "generated-rtl":
            style.text = css
            return
    style = ET.SubElement(head, f"{{{XHTML_NS}}}style", {"id": "generated-rtl", "type": "text/css"})
    style.text = css


def _update_navigation(
    document: SpineItem,
    toc_titles: list[str],
    book_title: str,
) -> None:
    if not toc_titles:
        return
    toc_nav = next(
        (node for node in document.root.iter()
         if local_name(node.tag) == "nav"
         and "toc" in node.get(f"{{{EPUB_NS}}}type", "").split()),
        None,
    )
    if toc_nav is None:
        return
    anchors = [node for node in toc_nav.iter() if local_name(node.tag) == "a"]
    for anchor, title in zip(anchors, toc_titles):
        _preserve_replace(anchor, title)
        _set_inline_rtl(anchor)
    for node in toc_nav.iter():
        if local_name(node.tag) in {"h1", "h2", "h3"} and book_title:
            _preserve_replace(node, book_title)
    for node in document.root.iter():
        if local_name(node.tag) == "title" and book_title:
            node.text = book_title
    document.root.set("lang", "ar")
    document.root.set(f"{{{XML_NS}}}lang", "ar")
    document.root.set("dir", "rtl")
    body = next((node for node in document.root.iter() if local_name(node.tag) == "body"), None)
    if body is not None:
        body.set("dir", "rtl")
        _set_inline_rtl(body)
    _set_rtl_styles(document.root)


def _update_ncx(root: ET.Element, toc_titles: list[str], book_title: str) -> None:
    if book_title:
        for node in root.iter():
            if local_name(node.tag) == "doctitle":
                text = next((child for child in node.iter() if local_name(child.tag) == "text"), None)
                if text is not None:
                    text.text = book_title
    labels: list[ET.Element] = []
    for point in root.iter():
        if local_name(point.tag) != "navpoint":
            continue
        nav_label = next(
            (child for child in list(point) if local_name(child.tag) == "navlabel"),
            None,
        )
        if nav_label is not None:
            label = next(
                (child for child in nav_label.iter() if local_name(child.tag) == "text"),
                None,
            )
            if label is not None:
                labels.append(label)
    for node, title in zip(labels, toc_titles):
        node.text = title


def _update_opf(root: ET.Element, title: str) -> None:
    metadata = root.find(f"{{{OPF_NS}}}metadata")
    if metadata is not None:
        language = metadata.findall(f"{{{DC_NS}}}language")
        if language:
            language[0].text = "ar"
            for extra in language[1:]:
                metadata.remove(extra)
        else:
            ET.SubElement(metadata, f"{{{DC_NS}}}language").text = "ar"
        if title:
            title_node = metadata.find(f"{{{DC_NS}}}title")
            if title_node is None:
                title_node = ET.SubElement(metadata, f"{{{DC_NS}}}title")
            title_node.text = title
    spine = root.find(f"{{{OPF_NS}}}spine")
    if spine is not None:
        spine.set("page-progression-direction", "rtl")


def _serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def rebuild_epub(original_epub: Path, translated_md: Path, output_epub: Path) -> tuple[int, int]:
    if original_epub.resolve() == output_epub.resolve():
        raise ValueError("Output EPUB must not overwrite the original EPUB.")
    markdown = translated_md.read_text(encoding="utf-8-sig")
    book_title, toc_titles, sections = translated_sections(markdown)
    replacements: dict[str, bytes] = {}

    with ZipFile(original_epub, "r") as source:
        opf_path, opf_root, manifest, spine_items = read_package(source)
        image_count = sum(
            1 for entry in source.infolist()
            if entry.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"))
        )
        pagebreak_count = 0
        for document in spine_items:
            if "nav" in document.properties:
                _update_navigation(document, toc_titles, book_title)
                replacements[document.path] = _serialize(document.root)
                continue
            section = sections.get(document.path)
            if section is None:
                raise ValueError(
                    f"Translated Markdown is missing the spine section for {document.path!r}."
                )
            marker_blocks = split_markdown_blocks(section)
            update_document(document, marker_blocks, book_title)
            pagebreak_count += sum(
                1 for node in document.root.iter()
                if _is_pagebreak(node)
            )
            replacements[document.path] = _serialize(document.root)

        for item_id, (path, media_type, _) in manifest.items():
            if media_type != "application/x-dtbncx+xml":
                continue
            ncx_root = parse_xml(source.read(path))
            _update_ncx(ncx_root, toc_titles, book_title)
            replacements[path] = _serialize(ncx_root)

        _update_opf(opf_root, book_title)
        replacements[opf_path] = _serialize(opf_root)

        output_epub.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(output_epub, "w") as output:
            for entry in source.infolist():
                data = replacements.get(entry.filename)
                if data is None:
                    data = source.read(entry.filename)
                if entry.filename == "mimetype":
                    entry.compress_type = ZIP_STORED
                output.writestr(entry, data)
    return image_count, pagebreak_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild an RTL EPUB from translated Markdown while retaining original page layout and assets."
    )
    parser.add_argument("original_epub", type=Path, help="Original EPUB used to preserve pages and images")
    parser.add_argument("translated_markdown", type=Path, help="Translated Markdown with spine document markers")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output EPUB (default: <original-name>.ar.epub)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.original_epub.is_file():
        print(f"Original EPUB not found: {args.original_epub}", file=sys.stderr)
        return 2
    if not args.translated_markdown.is_file():
        print(f"Translated Markdown not found: {args.translated_markdown}", file=sys.stderr)
        return 2
    output = args.output or args.original_epub.with_name(f"{args.original_epub.stem}.ar.epub")
    if output.resolve() == args.translated_markdown.resolve():
        print("Output EPUB must not overwrite the translated Markdown file.", file=sys.stderr)
        return 2
    try:
        images, pagebreaks = rebuild_epub(args.original_epub, args.translated_markdown, output)
    except (OSError, BadZipFile, ValueError, ET.ParseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"EPUB: {output}")
    print(f"Preserved original images: {images}; page-break markers: {pagebreaks}")
    print("Output language/direction: Arabic, right-to-left")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
