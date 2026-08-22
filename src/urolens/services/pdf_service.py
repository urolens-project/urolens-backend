"""Renders a patient-facing PDF lab report from a confirmed/released result."""
from datetime import datetime, timezone

from src.urolens.schemas.patient_portal import PatientResultDetail


def _safe_str(value: str | None, fallback: str = "Pending") -> str:
    # Placeholder text for an unset signature/label field in the PDF.
    return value if value else fallback


def _safe_val(value: int | None, fallback: int = 0) -> int:
    # Placeholder count (0) for an unset cell-count field in the PDF.
    return value if value is not None else fallback


def generate_result_pdf(result: PatientResultDetail, patient_name: str) -> bytes:
    """Render a one-page A4 PDF lab report for a patient result.

    Args:
        result: the result detail to render (cell counts, interpretation,
            signatures, etc.).
        patient_name: decrypted patient name to display; passed separately
            since `result` doesn't carry PII.

    Returns:
        The generated PDF as raw bytes.
    """
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=25 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CustomTitle", parent=styles["Title"], fontSize=16, spaceAfter=8
    )
    subtitle_style = ParagraphStyle(
        "CustomSubtitle", parent=styles["Normal"], fontSize=10, textColor=colors.grey
    )
    heading_style = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=16,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "CustomBody",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
    )
    footer_style = ParagraphStyle(
        "CustomFooter",
        parent=styles["Normal"],
        fontSize=7,
        textColor=colors.grey,
        alignment=TA_CENTER,
        leading=10,
    )
    sig_label_style = ParagraphStyle(
        "SigLabel",
        parent=styles["Normal"],
        fontSize=9,
        alignment=TA_CENTER,
        leading=12,
    )

    elements = []

    elements.append(Paragraph("UroLens Laboratory", title_style))
    elements.append(Paragraph("Urine Sediment Analysis Report", subtitle_style))
    elements.append(Spacer(1, 6 * mm))
    elements.append(
        Paragraph(f"Date Generated: {datetime.now(timezone.utc).strftime('%B %d, %Y')}", body_style)
    )
    elements.append(Spacer(1, 6 * mm))

    # ── Patient Information ──────────────────────────────────────────────────
    elements.append(Paragraph("Patient Information", heading_style))
    test_date = result.confirmed_at or result.created_at
    info_data = [
        ["Patient Name:", patient_name],
        ["Result ID:", str(result.result_id)],
        ["Date of Test:", test_date.strftime("%B %d, %Y") if test_date else "N/A"],
        ["Status:", result.status],
    ]
    info_table = Table(info_data, colWidths=[40 * mm, 100 * mm])
    info_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(info_table)
    elements.append(Spacer(1, 6 * mm))

    # ── Cell Count Results ───────────────────────────────────────────────────
    elements.append(Paragraph("Cell Count Results", heading_style))
    cc = result.cell_counts
    params = [
        ("Red Blood Cells (RBC)", _safe_val(cc.rbc if cc else None)),
        ("White Blood Cells (WBC)", _safe_val(cc.wbc if cc else None)),
        ("Epithelial Cells", _safe_val(cc.epithelial_cells if cc else None)),
        ("Casts", _safe_val(cc.casts if cc else None)),
        ("Bacteria", _safe_val(cc.bacteria if cc else None)),
        ("Crystals", _safe_val(cc.crystals if cc else None)),
        ("Mucus Threads", _safe_val(cc.mucus_threads if cc else None)),
    ]
    cell_data = [["Parameter", "Count"]]
    for param, count in params:
        cell_data.append([param, str(count)])
    cell_table = Table(cell_data, colWidths=[100 * mm, 40 * mm])
    cell_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f6fa")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(cell_table)
    elements.append(Spacer(1, 6 * mm))

    # ── Interpretation ───────────────────────────────────────────────────────
    elements.append(Paragraph("Interpretation", heading_style))
    interpretation_text = result.interpretation or "Pending review"
    elements.append(Paragraph(interpretation_text, body_style))
    elements.append(Spacer(1, 10 * mm))

    # ── Signatures ───────────────────────────────────────────────────────────
    sig_table = Table(
        [
            [
                Paragraph("Examined by:", sig_label_style),
                Paragraph("Reviewed by:", sig_label_style),
            ],
            [
                Paragraph("______________________", sig_label_style),
                Paragraph("______________________", sig_label_style),
            ],
            [
                Paragraph(_safe_str(result.medtech_name), sig_label_style),
                Paragraph(_safe_str(result.pathologist_name), sig_label_style),
            ],
            [
                Paragraph("Medical Technologist", sig_label_style),
                Paragraph("Pathologist", sig_label_style),
            ],
            [
                Paragraph("", sig_label_style),
                Paragraph(
                    f"License No: {_safe_str(result.pathologist_license)}",
                    sig_label_style,
                ),
            ],
        ],
        colWidths=[75 * mm, 75 * mm],
    )
    sig_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(sig_table)
    elements.append(Spacer(1, 10 * mm))

    # ── Footer ───────────────────────────────────────────────────────────────
    elements.append(Spacer(1, 8 * mm))
    elements.append(
        Paragraph(
            "This report is generated by UroLens. For clinical decisions, consult your physician.",
            footer_style,
        )
    )
    elements.append(Paragraph("CONFIDENTIAL — For patient use only", footer_style))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
