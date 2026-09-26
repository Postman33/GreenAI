"""Append a Blender preview sheet to an existing planting atlas PDF.

The four render paths are kept separate from the PDF layout so that a later
photorealistic render can replace them without recalculating the CAD plan.
"""

from __future__ import annotations

import argparse
import json
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


def register_fonts() -> None:
    windows_fonts = Path(r"C:\Windows\Fonts")
    pdfmetrics.registerFont(TTFont("PreviewArial", str(windows_fonts / "arial.ttf")))
    pdfmetrics.registerFont(TTFont("PreviewArialBold", str(windows_fonts / "arialbd.ttf")))


def append_preview(atlas: Path, renders: Path, output: Path) -> None:
    if atlas.resolve() == output.resolve():
        raise ValueError("Output must differ from source atlas")
    images = [
        ("overview_before.png", "ДО ПОСАДКИ"),
        ("overview_after.png", "ПОСЛЕ ПОСАДКИ"),
        ("pedestrian_after.png", "ВИД С УРОВНЯ ЧЕЛОВЕКА"),
        ("top_after.png", "ВИД СВЕРХУ"),
    ]
    missing = [name for name, _ in images if not (renders / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing Blender renders: {', '.join(missing)}")
    register_fonts()
    reader = PdfReader(str(atlas))
    width = float(reader.pages[0].mediabox.width)
    height = float(reader.pages[0].mediabox.height)
    sheet = BytesIO()
    page = canvas.Canvas(sheet, pagesize=(width, height))
    page.setFillColor(HexColor("#173c53"))
    page.setFont("PreviewArialBold", 17)
    page.drawString(30, height - 40, "Предпросмотр посадок в Blender")
    page.setFillColor(HexColor("#566a73"))
    page.setFont("PreviewArial", 8.5)
    page.drawString(30, height - 56, "Один фрагмент плана, одинаковая геометрия и камеры для сравнения")

    image_width = (width - 82) / 2
    image_height = image_width * 9 / 16
    columns = [30, 52 + image_width]
    row_tops = [height - 78, height - 313]
    for index, (name, label) in enumerate(images):
        x = columns[index % 2]
        top = row_tops[index // 2]
        y = top - image_height
        page.drawImage(str(renders / name), x, y, width=image_width,
                       height=image_height, preserveAspectRatio=True, mask="auto")
        page.setFillColor(HexColor("#173c53"))
        page.setFont("PreviewArialBold", 9)
        page.drawString(x, y - 14, label)
    page.setFillColor(HexColor("#566a73"))
    page.setFont("PreviewArial", 7.5)
    page.drawString(30, 18, "Схематичная 3D-визуализация. Высоты зданий и внешний вид растений условны; координаты посадок взяты из плана.")
    page.showPage()
    page.save()
    sheet.seek(0)

    writer = PdfWriter()
    for source_page in reader.pages:
        writer.add_page(source_page)
    writer.add_page(PdfReader(sheet).pages[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        writer.write(stream)


def append_gallery(atlas: Path, gallery_index: Path, output: Path) -> None:
    """Append one matched overview/pedestrian comparison sheet per place."""
    if atlas.resolve() == output.resolve():
        raise ValueError("Output must differ from source atlas")
    gallery = json.loads(gallery_index.read_text(encoding="utf-8"))
    if not gallery.get("places"):
        raise ValueError("Gallery index has no places")
    register_fonts()
    reader = PdfReader(str(atlas))
    writer = PdfWriter()
    for source_page in reader.pages:
        writer.add_page(source_page)
    width = float(reader.pages[0].mediabox.width)
    height = float(reader.pages[0].mediabox.height)
    image_width = (width - 82) / 2
    image_height = image_width * 9 / 16
    columns = [30, 52 + image_width]
    row_tops = [height - 78, height - 313]
    for number, place in enumerate(gallery["places"], start=1):
        renders = Path(place["renders"])
        images = [
            ("overview_before.png", "ОБЩИЙ ВИД - ДО"),
            ("overview_after.png", "ОБЩИЙ ВИД - ПОСЛЕ"),
            ("pedestrian_before.png", "С УРОВНЯ ЧЕЛОВЕКА - ДО"),
            ("pedestrian_after.png", "С УРОВНЯ ЧЕЛОВЕКА - ПОСЛЕ"),
        ]
        missing = [name for name, _ in images if not (renders / name).is_file()]
        if missing:
            raise FileNotFoundError(f"{place['id']}: missing {', '.join(missing)}")
        sheet = BytesIO()
        page = canvas.Canvas(sheet, pagesize=(width, height))
        page.setFillColor(HexColor("#173c53"))
        page.setFont("PreviewArialBold", 17)
        page.drawString(30, height - 40, f"Blender: участок {number} из {len(gallery['places'])}")
        counts = place["counts"]
        origin = place["origin"]
        page.setFillColor(HexColor("#566a73"))
        page.setFont("PreviewArial", 8.5)
        page.drawString(
            30, height - 56,
            f"X {origin['x']:.1f}, Y {origin['y']:.1f} | "
            f"новых деревьев: {counts['proposed_trees']} | "
            f"кустарников: {counts['proposed_shrub_instances']}",
        )
        for index, (name, label) in enumerate(images):
            x = columns[index % 2]
            y = row_tops[index // 2] - image_height
            page.drawImage(str(renders / name), x, y, width=image_width,
                           height=image_height, preserveAspectRatio=True, mask="auto")
            page.setFillColor(HexColor("#173c53"))
            page.setFont("PreviewArialBold", 9)
            page.drawString(x, y - 14, label)
        page.setFillColor(HexColor("#566a73"))
        page.setFont("PreviewArial", 7.5)
        page.drawString(
            30, 18,
            "В каждой паре камера и окружение одинаковы; меняются только проектируемые посадки. "
            "3D-модели и высоты условны.",
        )
        page.showPage()
        page.save()
        sheet.seek(0)
        writer.add_page(PdfReader(sheet).pages[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        writer.write(stream)


def main() -> None:
    parser = argparse.ArgumentParser(description="Append four Blender render previews to a planting atlas")
    parser.add_argument("--atlas", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--renders", type=Path)
    source.add_argument("--gallery-index", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.gallery_index:
        append_gallery(args.atlas, args.gallery_index, args.output)
    else:
        append_preview(args.atlas, args.renders, args.output)
    print(f"PDF with Blender preview: {args.output}")


if __name__ == "__main__":
    main()
