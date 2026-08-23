"""Renders a patient-facing PDF lab report from a confirmed/released result."""
from datetime import UTC, datetime

from src.urolens.schemas.patient_portal import PatientResultDetail


def _safeStr(value: str | None, fallback: str = "Pending") -> str:
    # Placeholder text for an unset signature/label field in the PDF.
    return value if value else fallback


def _safeVal(value: int | None, fallback: int = 0) -> int:
    # Placeholder count (0) for an unset cell-count field in the PDF.
    return value if value is not None else fallback


def generateResultPdf(result: PatientResultDetail, patientName: str) -> bytes:
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

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

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
    titleStyle = ParagraphStyle(
        "CustomTitle", parent=styles["Title"], fontSize=16, spaceAfter=8
    )
    subtitleStyle = ParagraphStyle(
        "CustomSubtitle", parent=styles["Normal"], fontSize=10, textColor=colors.grey
    )
    headingStyle = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=16,
        spaceAfter=6,
    )
    bodyStyle = ParagraphStyle(
        "CustomBody",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
    )
    footerStyle = ParagraphStyle(
        "CustomFooter",
        parent=styles["Normal"],
        fontSize=7,
        textColor=colors.grey,
        alignment=TA_CENTER,
        leading=10,
    )
    sigLabelStyle = ParagraphStyle(
        "SigLabel",
        parent=styles["Normal"],
        fontSize=9,
        alignment=TA_CENTER,
        leading=12,
    )

    elements = []

    elements.append(Paragraph("UroLens Laboratory", titleStyle))
    elements.append(Paragraph("Urine Sediment Analysis Report", subtitleStyle))
    elements.append(Spacer(1, 6 * mm))
    elements.append(
        Paragraph(f"Date Generated: {datetime.now(UTC).strftime('%B %d, %Y')}", bodyStyle)
    )
    elements.append(Spacer(1, 6 * mm))

    # ── Patient Information ──────────────────────────────────────────────────
    elements.append(Paragraph("Patient Information", headingStyle))
    testDate = result.confirmedAt or result.createdAt
    infoData = [
        ["Patient Name:", patientName],
        ["Result ID:", str(result.resultId)],
        ["Date of Test:", testDate.strftime("%B %d, %Y") if testDate else "N/A"],
        ["Status:", result.status],
    ]
    infoTable = Table(infoData, colWidths=[40 * mm, 100 * mm])
    infoTable.setStyle(
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
    elements.append(infoTable)
    elements.append(Spacer(1, 6 * mm))

    # ── Cell Count Results ───────────────────────────────────────────────────
    elements.append(Paragraph("Cell Count Results", headingStyle))
    cc = result.cellCounts
    params = [
        ("Red Blood Cells (RBC)", _safeVal(cc.rbc if cc else None)),
        ("White Blood Cells (WBC)", _safeVal(cc.wbc if cc else None)),
        ("Epithelial Cells", _safeVal(cc.epithelial_cells if cc else None)),
        ("Casts", _safeVal(cc.casts if cc else None)),
        ("Bacteria", _safeVal(cc.bacteria if cc else None)),
        ("Crystals", _safeVal(cc.crystals if cc else None)),
        ("Mucus Threads", _safeVal(cc.mucus_threads if cc else None)),
    ]
    cellData = [["Parameter", "Count"]]
    for param, count in params:
        cellData.append([param, str(count)])
    cellTable = Table(cellData, colWidths=[100 * mm, 40 * mm])
    cellTable.setStyle(
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
    elements.append(cellTable)
    elements.append(Spacer(1, 6 * mm))

    # ── Interpretation ───────────────────────────────────────────────────────
    elements.append(Paragraph("Interpretation", headingStyle))
    interpretationText = result.interpretation or "Pending review"
    elements.append(Paragraph(interpretationText, bodyStyle))
    elements.append(Spacer(1, 10 * mm))

    # ── Signatures ───────────────────────────────────────────────────────────
    sigTable = Table(
        [
            [
                Paragraph("Examined by:", sigLabelStyle),
                Paragraph("Reviewed by:", sigLabelStyle),
            ],
            [
                Paragraph("______________________", sigLabelStyle),
                Paragraph("______________________", sigLabelStyle),
            ],
            [
                Paragraph(_safeStr(result.medtechName), sigLabelStyle),
                Paragraph(_safeStr(result.pathologistName), sigLabelStyle),
            ],
            [
                Paragraph("Medical Technologist", sigLabelStyle),
                Paragraph("Pathologist", sigLabelStyle),
            ],
            [
                Paragraph("", sigLabelStyle),
                Paragraph(
                    f"License No: {_safeStr(result.pathologistLicense)}",
                    sigLabelStyle,
                ),
            ],
        ],
        colWidths=[75 * mm, 75 * mm],
    )
    sigTable.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(sigTable)
    elements.append(Spacer(1, 10 * mm))

    # ── Footer ───────────────────────────────────────────────────────────────
    elements.append(Spacer(1, 8 * mm))
    elements.append(
        Paragraph(
            "This report is generated by UroLens. For clinical decisions, consult your physician.",
            footerStyle,
        )
    )
    elements.append(Paragraph("CONFIDENTIAL — For patient use only", footerStyle))

    doc.build(elements)
    pdfBytes = buffer.getvalue()
    buffer.close()
    return pdfBytes
