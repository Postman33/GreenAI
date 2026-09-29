"""Export the organizer Markdown guide as a landscape A4 PDF."""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, LongTable, PageBreak, Paragraph,
    Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "for_organizers.md"
OUTPUT = ROOT / "output" / "pdf" / "Sylvitect-core_documentation.pdf"
PAGE_W, PAGE_H = landscape(A4)
BLUE = colors.HexColor("#2676b3")
NAVY = colors.HexColor("#142b43")
MUTED = colors.HexColor("#52697d")


def register_fonts():
    fonts = Path("C:/Windows/Fonts")
    if (fonts / "arial.ttf").exists():
        regular = fonts / "arial.ttf"
        bold = fonts / "arialbd.ttf"
        mono = fonts / "consola.ttf"
    else:
        from matplotlib.font_manager import FontProperties, findfont

        regular = Path(findfont(FontProperties(family="DejaVu Sans")))
        bold = Path(findfont(FontProperties(family="DejaVu Sans", weight="bold")))
        mono = Path(findfont(FontProperties(family="DejaVu Sans Mono")))
    pdfmetrics.registerFont(TTFont("Arial", str(regular)))
    pdfmetrics.registerFont(TTFont("Arial-Bold", str(bold)))
    pdfmetrics.registerFontFamily("Arial", normal="Arial", bold="Arial-Bold")
    pdfmetrics.registerFont(TTFont("Consolas", str(mono)))


def styles():
    return {
        "title": ParagraphStyle("title", fontName="Arial-Bold", fontSize=22,
            leading=26, textColor=NAVY, spaceAfter=12),
        "h2": ParagraphStyle("h2", fontName="Arial-Bold", fontSize=15,
            leading=18, textColor=NAVY, spaceBefore=15, spaceAfter=8,
            keepWithNext=True),
        "h3": ParagraphStyle("h3", fontName="Arial-Bold", fontSize=11.5,
            leading=14, textColor=BLUE, spaceBefore=11, spaceAfter=6,
            keepWithNext=True),
        "body": ParagraphStyle("body", fontName="Arial", fontSize=9.2,
            leading=13.4, textColor=NAVY, spaceAfter=6, alignment=TA_LEFT),
        "list": ParagraphStyle("list", fontName="Arial", fontSize=8.9,
            leading=12.8, textColor=NAVY, leftIndent=17, firstLineIndent=-11,
            spaceAfter=5),
        "cell": ParagraphStyle("cell", fontName="Arial", fontSize=7.65,
            leading=10.4, textColor=NAVY),
        "cell_head": ParagraphStyle("cell_head", fontName="Arial-Bold", fontSize=8,
            leading=10.4, textColor=colors.white),
        "caption": ParagraphStyle("caption", fontName="Arial", fontSize=8,
            leading=10, textColor=MUTED, spaceAfter=6),
    }


TOKEN = re.compile(r"(\[[^\]]+\]\([^)]*\)|\*\*[^*]+\*\*|`[^`]+`)")


def inline(source: str) -> str:
    result = []
    for item in TOKEN.split(source):
        if item.startswith("**") and item.endswith("**"):
            result.append(f"<b>{html.escape(item[2:-2])}</b>")
        elif item.startswith("`") and item.endswith("`"):
            result.append(f'<font color="#17699f">{html.escape(item[1:-1])}</font>')
        elif item.startswith("[") and "](" in item:
            label, target = item[1:].split("](", 1)
            target = target[:-1]
            if target.startswith(("https://", "http://")):
                result.append(f'<link href="{html.escape(target, quote=True)}" color="#17699f">{html.escape(label)}</link>')
            else:
                result.append(f"{html.escape(label)} ({html.escape(target)})")
        else:
            result.append(html.escape(item))
    return "".join(result)


def image_flowable(path: Path):
    from PIL import Image as PILImage

    with PILImage.open(path) as source:
        width, height = source.size
    width_pt = min(756, width / height * 340)
    height_pt = width_pt * height / width
    return Image(str(path), width=width_pt, height=height_pt)


def code_block(lines: list[str]):
    code = Preformatted("\n".join(lines), ParagraphStyle("code",
        fontName="Consolas", fontSize=8, leading=11, textColor=NAVY))
    table = Table([[code]], colWidths=[756])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf3f8")),
        ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#d0dce6")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return table


def table_flowable(lines: list[str], st):
    rows = []
    for line in lines:
        pieces = [part.strip() for part in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", part) for part in pieces):
            continue
        row_style = st["cell_head"] if not rows else st["cell"]
        rows.append([Paragraph(inline(part), row_style) for part in pieces])
    if not rows:
        return None
    widths = [155, 270, 331] if len(rows[0]) == 3 else [756 / len(rows[0])] * len(rows[0])
    table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f6fa")]),
        ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#cbd9e5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return table


def story_from_markdown(text: str):
    st = styles()
    story = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith(("## ", "### ")):
            image_index = index + 1
            while image_index < len(lines) and not lines[image_index].strip():
                image_index += 1
            diagram = re.fullmatch(r"<!-- diagram:([a-z_]+):start -->", lines[image_index].strip()) if image_index < len(lines) else None
            if diagram:
                name = diagram.group(1)
                end_marker = f"<!-- diagram:{name}:end -->"
                end_index = next(i for i in range(image_index + 1, len(lines))
                    if lines[i].strip() == end_marker)
                heading_style = st["h2"] if line.startswith("## ") else st["h3"]
                heading_text = line[3:] if line.startswith("## ") else line[4:]
                story.append(KeepTogether([
                    Paragraph(inline(heading_text), heading_style),
                    image_flowable(SOURCE.parent / "diagrams" / f"{name}.png"),
                ]))
                story.append(Spacer(1, 5))
                index = end_index + 1
                continue
            match = re.match(r"!\[(.*?)\]\((.*?)\)", lines[image_index].strip()) if image_index < len(lines) else None
            if match:
                heading_style = st["h2"] if line.startswith("## ") else st["h3"]
                heading_text = line[3:] if line.startswith("## ") else line[4:]
                story.append(KeepTogether([
                    Paragraph(inline(heading_text), heading_style),
                    image_flowable(SOURCE.parent / match.group(2)),
                    Paragraph(match.group(1), st["caption"]),
                ]))
                story.append(Spacer(1, 5))
                index = image_index + 1
                continue
        diagram = re.fullmatch(r"<!-- diagram:([a-z_]+):start -->", line)
        if diagram:
            name = diagram.group(1)
            end_marker = f"<!-- diagram:{name}:end -->"
            end_index = next(i for i in range(index + 1, len(lines))
                if lines[i].strip() == end_marker)
            story.extend([image_flowable(SOURCE.parent / "diagrams" / f"{name}.png"), Spacer(1, 5)])
            index = end_index + 1
            continue
        if line == "<!-- pagebreak -->":
            story.append(PageBreak())
            index += 1
            continue
        if line.startswith("<!--"):
            index += 1
            continue
        if line.startswith("```mermaid"):
            raise ValueError("Mermaid block must be enclosed by diagram markers")
        if line.startswith("```"):
            block = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            story.extend([Spacer(1, 5), code_block(block), Spacer(1, 7)])
        elif line.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            table = table_flowable(table_lines, st)
            if table:
                story.extend([table, Spacer(1, 8)])
            continue
        elif line.startswith("!["):
            match = re.match(r"!\[(.*?)\]\((.*?)\)", line)
            if match:
                story.extend([KeepTogether([
                    image_flowable(SOURCE.parent / match.group(2)),
                    Paragraph(match.group(1), st["caption"]),
                ]), Spacer(1, 5)])
        elif line.startswith("# "):
            story.extend([Paragraph(inline(line[2:]), st["title"]),
                HRFlowable(width="100%", color=BLUE, thickness=1), Spacer(1, 8)])
        elif line.startswith("## "):
            story.append(Paragraph(inline(line[3:]), st["h2"]))
        elif line.startswith("### "):
            story.append(Paragraph(inline(line[4:]), st["h3"]))
        elif re.match(r"^-\s", line):
            story.append(Paragraph("&#8226;  " + inline(line[2:]), st["list"]))
        elif re.match(r"^\d+\.\s", line):
            num, rest = line.split(". ", 1)
            story.append(Paragraph(html.escape(num) + ".  " + inline(rest), st["list"]))
        else:
            parts = [line]
            while index + 1 < len(lines):
                nxt = lines[index + 1].strip()
                if not nxt or nxt.startswith(("#", "!", "|", "```", "- ")) or re.match(r"^\d+\.\s", nxt):
                    break
                index += 1
                parts.append(nxt)
            story.append(KeepTogether([Paragraph(inline(" ".join(parts)), st["body"])]))
        index += 1
    # A trailing table spacer can otherwise create an empty final page.
    while story and isinstance(story[-1], Spacer):
        story.pop()
    return story


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#d0dce6"))
    canvas.line(42, 33, PAGE_W - 42, 33)
    canvas.setFont("Arial", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(42, 21, "Sylvitect-core  |  Архитектура и алгоритмы  |  29.09.2026")
    canvas.drawRightString(PAGE_W - 42, 21, str(doc.page))
    canvas.restoreState()


def main():
    register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(str(OUTPUT), pagesize=(PAGE_W, PAGE_H),
        leftMargin=42, rightMargin=42, topMargin=38, bottomMargin=48,
        title="Sylvitect-core: архитектура и алгоритмы",
        author="Sylvitect-core team")
    document.build(story_from_markdown(SOURCE.read_text(encoding="utf-8")),
        onFirstPage=footer, onLaterPages=footer)
    print(OUTPUT, OUTPUT.stat().st_size)


if __name__ == "__main__":
    main()
