"""Build a human-readable PDF delivery report for one planting pipeline run."""

from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from shapely.geometry import shape


PAGE_SIZE = landscape(A4)
TARGET_ZONE_LABEL = "USDA 4 (Москва)"
NPA_URLS = {
    "СП 42.13330.2026": "https://protect.gost.ru/sp/details/f6917ab4-63d8-4ecb-9794-0b4990ba3b99",
    "743-ПП": "https://www.mos.ru/upload/documents/files/7389/Postanovlenie743-PP.pdf",
}
TARGET_LABELS = {
    "building": "здание", "heat_pipe": "теплосеть", "road_edge": "край дороги",
    "sidewalk": "тротуар", "water_pipe": "водопровод", "gas_pipe": "газопровод",
    "power_cable": "силовой кабель", "existing_tree": "существующее дерево",
    "overhead_power_line": "воздушная ЛЭП", "sewer_pipe": "канализация",
    "storm_drain": "водосток", "telecom_cable": "кабель связи",
    "auto_tree": "другое дерево", "auto_shrub": "другой кустарник",
}
PLANT_TYPE_LABELS = {"tree": "дерево", "shrub": "кустарник", "herbaceous": "травянистое покрытие"}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_decisions(path: Path) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                feature = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Line {line_number}: invalid planting decision JSON"
                ) from error
            if feature.get("type") != "Feature":
                raise ValueError(f"Line {line_number}: expected GeoJSON Feature")
            decisions.append(feature)
    return decisions


def read_plan(path: Path) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            if feature.get("type") != "Feature" or not feature.get("properties", {}).get("planting_id"):
                raise ValueError(f"{path}:{line_number}: expected a planting Feature with ID")
            features.append(feature)
    return features


def read_existing_tree_audit(path: Path) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            feature = json.loads(line)
            if feature.get("type") != "Feature" or feature.get("properties", {}).get("object_type") != "existing_tree_rule_screening":
                raise ValueError(f"{path}:{line_number}: expected existing tree screening Feature")
            features.append(feature)
    return features


def normative_references(checks: list[dict[str, Any]]) -> list[str]:
    references: list[str] = []
    for check in checks:
        reference = str(check.get("norm_reference") or "").strip()
        if (reference.startswith("СП ") and ("таблиц" in reference or "п." in reference)) or (
            "Постановление" in reference and "743-ПП" in reference and "п." in reference
        ):
            if reference not in references:
                references.append(reference)
    return references


def normative_link(reference: str) -> str:
    escaped = html.escape(reference)
    for marker, url in NPA_URLS.items():
        if marker in reference:
            return f'<link href="{html.escape(url, quote=True)}" color="#176B6A">{escaped}</link>'
    return escaped


def existing_tree_conflict_rows(
    features: Iterable[dict[str, Any]], cell_style: ParagraphStyle,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for feature in features:
        properties = feature.get("properties", {})
        if properties.get("status") != "conflict":
            continue
        failed = [check for check in properties.get("checks", []) if check.get("status") == "conflict"]
        coordinates = feature.get("geometry", {}).get("coordinates", [])
        if len(coordinates) < 2 or not failed:
            continue
        rows.append([
            paragraph(html.escape(str(properties.get("existing_tree_id", feature.get("id", "-")))), cell_style),
            paragraph(f"X {float(coordinates[0]):.2f}<br/>Y {float(coordinates[1]):.2f}", cell_style),
            paragraph("<br/>".join(target_label(check) for check in failed), cell_style),
            paragraph("<br/>".join(
                f"{float(check['actual_distance_m']):.2f} / {float(check['required_distance_m']):.2f} м"
                for check in failed
            ), cell_style),
            paragraph("<br/>".join(html.escape(str(check.get("code", "-"))) for check in failed), cell_style),
            paragraph("<br/>".join(normative_link(str(check.get("norm_reference") or "-")) for check in failed), cell_style),
        ])
    return rows


def target_label(check: dict[str, Any]) -> str:
    target = str(check.get("target") or "")
    if not target:
        code = str(check.get("code") or "")
        for fragment, object_type in (("_GAS_", "gas_pipe"), ("_WATER_", "water_pipe"),
                                      ("_HEAT_", "heat_pipe"), ("_BUILDING_", "building")):
            if fragment in code:
                target = object_type
                break
        if not target:
            target = code or "?"
    return html.escape(TARGET_LABELS.get(target, target))


def find_font_files() -> tuple[Path, Path]:
    custom_dir = os.getenv("SYLVITECT_PDF_FONT_DIR")
    candidates: list[tuple[Path, Path]] = []
    if custom_dir:
        root = Path(custom_dir)
        candidates.append((root / "DejaVuSans.ttf", root / "DejaVuSans-Bold.ttf"))
        candidates.append((root / "arial.ttf", root / "arialbd.ttf"))
    candidates.extend(
        [
            (
                Path("C:/Windows/Fonts/arial.ttf"),
                Path("C:/Windows/Fonts/arialbd.ttf"),
            ),
            (
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ),
        ]
    )
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            return regular, bold
    raise RuntimeError(
        "No Cyrillic TrueType font found. Install DejaVu Sans or set "
        "SYLVITECT_PDF_FONT_DIR."
    )


def register_fonts() -> tuple[str, str]:
    regular, bold = find_font_files()
    pdfmetrics.registerFont(TTFont("Sylvitect-core", regular))
    pdfmetrics.registerFont(TTFont("Sylvitect-core-Bold", bold))
    return "Sylvitect-core", "Sylvitect-core-Bold"


def paragraph(text: Any, style: ParagraphStyle) -> Paragraph:
    value = str(text if text is not None else "-")
    value = value.replace("\n", "<br/>")
    return Paragraph(value, style)


def format_distance(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f} м"


def failed_checks(properties: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check
        for check in properties.get("checks", [])
        if check.get("status") == "failed"
    ]


def rejection_rows(
    decisions: Iterable[dict[str, Any]], cell_style: ParagraphStyle
) -> tuple[list[list[Any]], Counter[str]]:
    rows: list[list[Any]] = []
    cause_counts: Counter[str] = Counter()
    type_labels = {"tree": "Дерево", "shrub": "Кустарник", "herbaceous": "Травы"}
    for feature in decisions:
        properties = feature.get("properties", {})
        if properties.get("status") != "rejected":
            continue
        failures = failed_checks(properties)
        codes = [str(item.get("code", "UNKNOWN")) for item in failures]
        cause_counts.update(codes)
        metrics = []
        for check in failures:
            actual = format_distance(check.get("actual_distance_m"))
            required = format_distance(check.get("required_distance_m"))
            deficit = "-"
            if (
                check.get("actual_distance_m") is not None
                and check.get("required_distance_m") is not None
            ):
                missing = max(
                    0.0,
                    float(check["required_distance_m"])
                    - float(check["actual_distance_m"]),
                )
                deficit = f"не хватает {missing:.2f} м" if missing > 0 else "граница зоны"
            metrics.append(f"{check.get('code', '?')}: {actual} / {required}; {deficit}")
        reasons = properties.get("rejection_reasons") or [
            str(item.get("explanation", "Причина не указана")) for item in failures
        ]
        coordinates = feature.get("geometry", {}).get("coordinates", [None, None])
        coordinate_text = (
            f"X {float(coordinates[0]):.2f}<br/>Y {float(coordinates[1]):.2f}"
            if len(coordinates) >= 2 and coordinates[0] is not None
            else "-"
        )
        rows.append(
            [
                paragraph(properties.get("candidate_id", "-"), cell_style),
                paragraph(
                    f"{type_labels.get(properties.get('plant_type'), properties.get('plant_type', '-'))}<br/>"
                    f"{properties.get('species', '-')}",
                    cell_style,
                ),
                paragraph(coordinate_text, cell_style),
                paragraph("<br/>".join(codes) or "-", cell_style),
                paragraph("<br/>".join(metrics) or "-", cell_style),
                paragraph("<br/>".join(str(item) for item in reasons), cell_style),
            ]
        )
    return rows, cause_counts


def planting_explanation_rows(
    plan: Iterable[dict[str, Any]], cell_style: ParagraphStyle
) -> tuple[list[list[Any]], int]:
    """One auditable explanation and an exact NPA clause for every plan feature."""
    rows: list[list[Any]] = []
    manual_count = 0
    for feature in plan:
        properties = feature["properties"]
        planting_id = str(properties["planting_id"])
        checks = properties.get("checks") or []
        references = normative_references(checks)
        if not references:
            raise ValueError(f"Planting {planting_id} has no NPA reference with a clause/table")
        status = str(properties.get("status") or "")
        if status not in {"accepted", "manual_review"}:
            raise ValueError(f"Planting {planting_id} has unexpected status {status!r}")
        if status == "manual_review":
            manual_count += 1
        geometry = feature.get("geometry", {})
        coordinates = geometry.get("coordinates", [])
        if geometry.get("type") == "Point" and len(coordinates) >= 2:
            location = f"X {float(coordinates[0]):.2f}; Y {float(coordinates[1]):.2f}"
        else:
            polygon = shape(geometry)
            units = float(properties.get("dxf_units_per_meter") or 1)
            location = (f"Контур {polygon.area / units ** 2:.2f} м²; "
                        f"центр X {polygon.centroid.x:.2f}, Y {polygon.centroid.y:.2f}")
        source = (properties.get("species_selection") or {}).get("source")
        source_label = {"preset_profile": "профиль", "explicit_request": "запрос"}.get(source, "не указан")
        layout = properties.get("layout_style") or properties.get("design_style") or "-"
        identity = (
            f"<b>{html.escape(planting_id)}</b><br/>"
            f"{html.escape(str(properties.get('species') or '-'))}<br/>"
            f"{html.escape(location)}<br/>"
            f"Вид: {source_label}; схема: {html.escape(str(layout))}"
        )
        measured = [
            check for check in checks
            if check.get("actual_distance_m") is not None
            and check.get("required_distance_m") is not None
            and check.get("status") == "passed"
        ]
        measured.sort(key=lambda check: float(check["actual_distance_m"])
                      - float(check["required_distance_m"]))
        facts = [
            f"{target_label(check)}: "
            f"{float(check['actual_distance_m']):.2f} / {float(check['required_distance_m']):.2f} м"
            for check in measured
        ]
        if geometry.get("type") == "Point":
            conclusion = "Центр посадки внутри допустимой зоны; проверенные расстояния соблюдены."
        else:
            conclusion = "Контур находится внутри рассчитанной допустимой зоны."
        if facts:
            conclusion += " Пройденные проверки (факт / минимум): " + "; ".join(facts) + "."
        review_checks = [check for check in checks if check.get("status") == "manual_review"]
        if review_checks:
            review_labels = [
                f"{target_label(check)} "
                f"({html.escape(str(check.get('code') or '?'))})"
                for check in review_checks
            ]
            conclusion += " Требуется ручная проверка: " + ", ".join(review_labels) + "."
        if geometry.get("type") != "Point":
            applied_rules = [str(check.get("code")) for check in checks
                             if check.get("geometry_source") and check.get("status") == "passed"]
            if applied_rules:
                conclusion += f" Применено площадных ограничений: {len(applied_rules)}."
        npa = "<br/>".join(normative_link(reference) for reference in references)
        rows.append([
            paragraph(identity, cell_style),
            paragraph(conclusion, cell_style),
            paragraph(npa, cell_style),
        ])
    return rows, manual_count


def page_decorator(canvas: Any, document: Any) -> None:
    canvas.saveState()
    width, height = PAGE_SIZE
    canvas.setStrokeColor(colors.HexColor("#B8C8D8"))
    canvas.setLineWidth(0.5)
    canvas.line(14 * mm, height - 11 * mm, width - 14 * mm, height - 11 * mm)
    canvas.setFont("Sylvitect-core", 7)
    canvas.setFillColor(colors.HexColor("#526579"))
    canvas.drawString(14 * mm, 7 * mm, "Sylvitect-core - отчёт по плану озеленения")
    canvas.drawRightString(width - 14 * mm, 7 * mm, f"Страница {document.page}")
    canvas.restoreState()


def build_pdf(
    decisions_path: Path,
    plan_report_path: Path,
    zone_report_path: Path,
    verification_report_path: Path,
    output_path: Path,
    input_dxf: Path | None = None,
    preview_path: Path | None = None,
    plan_path: Path | None = None,
    existing_tree_audit_path: Path | None = None,
) -> Path:
    regular_font, bold_font = register_fonts()
    decisions = read_decisions(decisions_path)
    plan_report = read_json(plan_report_path)
    if plan_path is None and plan_report.get("output"):
        reported_path = Path(str(plan_report["output"]))
        plan_path = reported_path if reported_path.exists() else plan_report_path.parent / reported_path
    if plan_path is None or not plan_path.is_file():
        raise ValueError("Planting plan is required for per-planting PDF explanations")
    plan = read_plan(plan_path)
    existing_tree_audit = (
        read_existing_tree_audit(existing_tree_audit_path)
        if existing_tree_audit_path is not None else []
    )
    reported_count = plan_report.get("feature_count")
    if reported_count is not None and int(reported_count) != len(plan):
        raise ValueError("Planting plan feature count differs from the plan report")
    if len({item["properties"]["planting_id"] for item in plan}) != len(plan):
        raise ValueError("Planting plan contains duplicate IDs")
    zone_report = read_json(zone_report_path)
    verification = read_json(verification_report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "GreenTitle", parent=styles["Title"], fontName=bold_font,
        fontSize=20, leading=24, textColor=colors.HexColor("#143B5D"),
        spaceAfter=5 * mm, alignment=TA_LEFT,
    )
    heading = ParagraphStyle(
        "GreenHeading", parent=styles["Heading1"], fontName=bold_font,
        fontSize=14, leading=17, textColor=colors.HexColor("#176B6A"),
        spaceBefore=3 * mm, spaceAfter=3 * mm,
    )
    subheading = ParagraphStyle(
        "GreenSubheading", parent=styles["Heading2"], fontName=bold_font,
        fontSize=10, leading=12, textColor=colors.HexColor("#143B5D"),
        spaceBefore=2 * mm, spaceAfter=1.5 * mm,
    )
    body = ParagraphStyle(
        "GreenBody", parent=styles["BodyText"], fontName=regular_font,
        fontSize=8.5, leading=11, textColor=colors.HexColor("#263746"),
    )
    small = ParagraphStyle(
        "GreenSmall", parent=body, fontSize=6.5, leading=8,
    )
    table_header = ParagraphStyle(
        "GreenTableHeader", parent=small, fontName=bold_font,
        textColor=colors.white, leading=7.5,
    )
    appendix_style = ParagraphStyle(
        "GreenAppendix", parent=small, fontSize=7.1, leading=9.3,
    )
    explanation_rows, explained_manual_count = planting_explanation_rows(plan, appendix_style)

    document = SimpleDocTemplate(
        str(output_path), pagesize=PAGE_SIZE,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=16 * mm, bottomMargin=13 * mm,
        title="Sylvitect-core - отчёт по плану озеленения",
        author="Sylvitect-core",
    )
    story: list[Any] = []
    story.append(paragraph("Отчёт по плану озеленения", title))
    source_name = input_dxf.name if input_dxf else "не указан"
    story.append(paragraph(
        f"Исходный чертёж: <b>{source_name}</b><br/>"
        f"Дата формирования: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M')}<br/>"
        f"Система координат: локальные координаты DXF; масштаб: "
        f"{zone_report.get('dxf_units_per_meter', 1):g} ед./м",
        body,
    ))
    story.append(Spacer(1, 4 * mm))

    rejected_rows, cause_counts = rejection_rows(decisions, small)
    story.append(paragraph("1. Причины отклонения кандидатов", heading))
    story.append(paragraph(
        "Таблица связывает красные диагностические точки в DXF с конкретными "
        "нарушенными проверками. Поиск выполняется по идентификатору кандидата.",
        body,
    ))
    story.append(Spacer(1, 2 * mm))
    headers = ["ID", "Тип и растение", "Координаты", "Проверки", "Факт / требование", "Причина"]
    data = [[paragraph(item, table_header) for item in headers], *rejected_rows]
    rejection_table = LongTable(
        data,
        colWidths=[19 * mm, 31 * mm, 25 * mm, 34 * mm, 55 * mm, 103 * mm],
        repeatRows=1,
        splitByRow=True,
    )
    rejection_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176B6A")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C8D8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F7F8")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(rejection_table)
    if existing_tree_audit_path is not None:
        story.append(Spacer(1, 4 * mm))
        story.append(paragraph("Существующие деревья: потенциальные конфликты", heading))
        conflict_count = sum(
            feature.get("properties", {}).get("status") == "conflict"
            for feature in existing_tree_audit
        )
        incomplete_count = sum(
            feature.get("properties", {}).get("status") == "incomplete"
            for feature in existing_tree_audit
        )
        story.append(paragraph(
            f"Проверено существующих деревьев: {len(existing_tree_audit)}; "
            f"с потенциальными конфликтами: {conflict_count}; "
            f"с неполной проверкой: {incomplete_count}. "
            "Сравниваются координаты стволов с действующими проектными отступами для новых деревьев. "
            "Это перечень для натурной проверки положения деревьев и сетей, а не решение об удалении деревьев.",
            body,
        ))
        unchecked_utilities = zone_report.get("plant_types", {}).get("tree", {}).get(
            "unchecked_utility_object_types", []
        )
        if unchecked_utilities:
            story.append(paragraph(
                "Для части сетей нет активного правила отступа, поэтому они не входят в эту проверку: "
                + ", ".join(
                    html.escape(TARGET_LABELS.get(str(target), str(target)))
                    for target in unchecked_utilities
                ) + ".",
                body,
            ))
        target_counts: Counter[str] = Counter(
            target
            for feature in existing_tree_audit
            for target in {
                str(check.get("target", "?"))
                for check in feature.get("properties", {}).get("checks", [])
                if check.get("status") == "conflict"
            }
        )
        if target_counts:
            story.append(paragraph(
                "По типам ограничений (одно дерево может входить в несколько групп): "
                + "; ".join(
                    f"{html.escape(TARGET_LABELS.get(target, target))} — {count}"
                    for target, count in sorted(target_counts.items())
                ) + ".",
                body,
            ))
        story.append(Spacer(1, 2 * mm))
        tree_headers = ["ID", "Координаты", "Объект", "Факт / минимум", "Правило", "НПА и пункт"]
        tree_rows = existing_tree_conflict_rows(existing_tree_audit, small)
        tree_data = [[paragraph(item, table_header) for item in tree_headers], *tree_rows]
        if not tree_rows:
            tree_data.append([paragraph("Потенциальные конфликты не выявлены", small), *[""] * 5])
        tree_table = LongTable(
            tree_data, colWidths=[20 * mm, 31 * mm, 39 * mm, 34 * mm, 45 * mm, 95 * mm],
            repeatRows=1, splitByRow=True,
        )
        tree_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#A65B16")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D6E2E7")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FFF8F0")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.2),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        story.append(tree_table)
    story.append(PageBreak())

    story.append(paragraph("2. Сводка результата", heading))
    status_counts = Counter(
        (str(feature["properties"].get("request_id", "")),
         str(feature["properties"].get("status", "")))
        for feature in plan
    )
    summary_rows = [[
        paragraph("Сценарий", table_header), paragraph("Тип", table_header),
        paragraph("Растение", table_header), paragraph("Подтверждено", table_header),
        paragraph("На проверке", table_header),
        paragraph("Диагностических отказов", table_header),
    ]]
    for request_id, item in plan_report.get("summary", {}).items():
        summary_rows.append([
            paragraph(request_id, body), paragraph(item.get("plant_type", "-"), body),
            paragraph(item.get("species", "-"), body),
            paragraph(status_counts[(request_id, "accepted")], body),
            paragraph(status_counts[(request_id, "manual_review")], body),
            paragraph(item.get("diagnostic_rejected_count", 0), body),
        ])
    summary_table = Table(summary_rows, colWidths=[42 * mm, 28 * mm, 65 * mm, 27 * mm, 32 * mm, 55 * mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#143B5D")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C8D8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F7F8")]),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 4 * mm))

    status_color = "#277A4B" if verification.get("status") == "passed" else "#B63737"
    status_text = "ПРОЙДЕНА" if verification.get("status") == "passed" else "НЕ ПРОЙДЕНА"
    status_box = Table([[paragraph(
        f"Техническая верификация: <font color='{status_color}'><b>{status_text}</b></font><br/>"
        f"Точек посадки: {plan_report.get('point_placement_count', 0)}; "
        f"площадных посадок: {plan_report.get('area_placement_count', 0)}; "
        f"требуют ручной проверки: {plan_report.get('manual_review_count', 0)}. "
        "Верификация относится к доступным геометрическим проверкам и целостности результата.", body
    )]], colWidths=[240 * mm])
    status_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor(status_color)),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F5FAF6")),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(status_box)

    composition_advisories = plan_report.get("composition_advisories") or []
    if composition_advisories:
        story.append(Spacer(1, 4 * mm))
        story.append(paragraph("Композиция: существующие растения", heading))
        story.append(paragraph(
            "Это рекомендации для проверки на месте. Существующие растения не удаляются автоматически.",
            body,
        ))
        composition_rows = [[
            paragraph("Растение", table_header), paragraph("Координаты DXF", table_header),
            paragraph("Рекомендация", table_header),
        ]]
        for advisory in composition_advisories:
            x, y = advisory["coordinates"]
            is_tree = advisory.get("plant_type") == "tree"
            action = (
                f"Предложено удалить дерево: мешает {advisory.get('blocked_group_stations', '?')} "
                f"из {advisory.get('group_size', '?')} мест новой группы. "
                f"Баллы сохранения/замены: {advisory.get('keep_tree_design_score', '?')} / "
                f"{advisory.get('remove_tree_design_score', '?')}. "
                "Проверить дерево и согласовать решение"
                if is_tree else
                "Убрать одиночный куст из композиции: пересадка или удаление после проверки"
                if advisory["recommendation"] == "propose_removal_from_composition"
                else "Определить вид куста до решения о пересадке"
            )
            identifier = advisory.get("existing_tree_id") if is_tree else advisory.get("existing_shrub_id")
            suffix = "" if is_tree else f"; посадка {html.escape(str(advisory['planting_id']))}"
            composition_rows.append([
                paragraph(html.escape(str(identifier)), body),
                paragraph(f"{x:.2f}; {y:.2f}", body),
                paragraph(f"{action}{suffix}", body),
            ])
        composition_table = Table(composition_rows, colWidths=[40 * mm, 62 * mm, 138 * mm],
                                  repeatRows=1)
        composition_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#143B5D")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C8D8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F7F8")]),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(composition_table)

    unchecked_lines = []
    for plant_type, item in zone_report.get("plant_types", {}).items():
        plant_label = PLANT_TYPE_LABELS.get(plant_type, plant_type)
        unavailable = [
            f"{target_label({'target': rule.get('target_object_type')})} "
            f"({html.escape(str(rule.get('rule_code') or '?'))})"
            for rule in item.get("rules", []) if rule.get("status") == "unavailable"
        ]
        if unavailable:
            unchecked_lines.append(
                f"{plant_label}: активные правила без пригодной геометрии - "
                + ", ".join(unavailable) + "."
            )
        without_rule = item.get("unchecked_utility_object_types") or []
        if without_rule:
            unchecked_lines.append(
                f"{plant_label}: сети без активного правила - "
                + ", ".join(target_label({"target": target}) for target in without_rule) + "."
            )
    if unchecked_lines:
        story.append(Spacer(1, 3 * mm))
        unchecked_box = Table([[paragraph(
            "<b>Не проверено автоматически</b><br/>" + "<br/>".join(unchecked_lines), body
        )]], colWidths=[240 * mm])
        unchecked_box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#B46A20")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7E8")),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(unchecked_box)

    if preview_path and preview_path.is_file():
        story.append(Spacer(1, 4 * mm))
        image = Image(str(preview_path))
        max_width, max_height = 250 * mm, 115 * mm
        ratio = min(max_width / image.imageWidth, max_height / image.imageHeight)
        image.drawWidth = image.imageWidth * ratio
        image.drawHeight = image.imageHeight * ratio
        story.append(image)

    story.append(PageBreak())
    story.append(paragraph("3. Частые причины отказа", heading))
    cause_data = [[paragraph("Проверка", table_header), paragraph("Количество", table_header)]]
    for code, count in cause_counts.most_common():
        cause_data.append([paragraph(code, body), paragraph(count, body)])
    cause_table = Table(cause_data, colWidths=[130 * mm, 35 * mm], repeatRows=1)
    cause_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176B6A")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C8D8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F7F8")]),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(cause_table)

    story.append(Spacer(1, 4 * mm))
    story.append(paragraph("4. Допустимые зоны и правила", heading))
    rule_rows = [[
        paragraph("Тип", table_header), paragraph("Площадь, м²", table_header),
        paragraph("Статус", table_header), paragraph("Применённые правила", table_header),
    ]]
    for plant_type, item in zone_report.get("plant_types", {}).items():
        rules = [
            rule.get("rule_code", "-")
            for rule in item.get("rules", [])
            if rule.get("status") == "applied"
        ]
        rule_rows.append([
            paragraph(plant_type, body),
            paragraph(f"{float(item.get('allowed_area_in_dxf_square_units', 0)):.2f}", body),
            paragraph(item.get("verification_status", "-"), body),
            paragraph(", ".join(rules) or "-", body),
        ])
    rule_table = Table(rule_rows, colWidths=[32 * mm, 35 * mm, 55 * mm, 135 * mm], repeatRows=1)
    rule_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#143B5D")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C8D8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(rule_table)

    story.append(Spacer(1, 4 * mm))
    story.append(paragraph("5. Климатический отбор растений", heading))
    story.append(paragraph(
        f"Расчётный климатический профиль: <b>{TARGET_ZONE_LABEL}</b>. "
        "Виды со статусом seasonal_only исключаются из автоматического каталога "
        "многолетних посадок; conditional требуют подтверждения сорта, партии и "
        "микроклимата участка.", body,
    ))

    story.append(Spacer(1, 4 * mm))
    story.append(paragraph("6. Комплект результата", heading))
    files = [
        output_path.name,
        "result_with_planting_plan.dxf или planting_overlay.dxf",
        "planting_diagnostics.dxf",
        "planting_decisions.geojsonl",
        "verification_report.json",
    ]
    story.append(paragraph("<br/>".join(f"- {item}" for item in files), body))

    story.append(PageBreak())
    story.append(paragraph("7. Объяснение каждой посадки", heading))
    story.append(paragraph(
        f"Приведены все {len(plan)} объекта плана, включая {explained_manual_count} со статусом ручной проверки. "
        "Показаны все пройденные проверки, начиная с ближайших к порогу; факт / минимум дан в метрах. "
        "Ссылка на НПА указывает конкретную таблицу или пункт. "
        "Источник вида и схема отражают проектный выбор, а не отдельную норму. "
        "Полный журнал проверок каждого ID находится в planting_plan.geojsonl.",
        body,
    ))
    story.append(Spacer(1, 3 * mm))
    appendix_headers = ["Посадка и место", "Почему предложена / что проверить", "НПА и пункт"]
    appendix_data = [[paragraph(item, table_header) for item in appendix_headers], *explanation_rows]
    appendix_table = LongTable(
        appendix_data, colWidths=[58 * mm, 129 * mm, 77 * mm],
        repeatRows=1, splitByRow=True,
    )
    appendix_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#143B5D")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#143B5D")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.2, colors.HexColor("#D6E2E7")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F7F8")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(appendix_table)

    document.build(story, onFirstPage=page_decorator, onLaterPages=page_decorator)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"PDF report was not created: {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Sylvitect-core PDF report.")
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--planting-plan", type=Path, required=True)
    parser.add_argument("--plan-report", type=Path, required=True)
    parser.add_argument("--zone-report", type=Path, required=True)
    parser.add_argument("--verification-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-dxf", type=Path)
    parser.add_argument("--existing-tree-audit", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    result = build_pdf(
        args.decisions,
        args.plan_report,
        args.zone_report,
        args.verification_report,
        args.output,
        args.input_dxf,
        args.preview,
        args.planting_plan,
        args.existing_tree_audit,
    )
    print(f"PDF report: {result}")


if __name__ == "__main__":
    main()
