"""Renders a patient-facing PDF lab report from a confirmed/released result."""
from datetime import UTC, datetime

from src.schemas.patient_portal import PatientResultDetail


def _safeStr(value: str | None, fallback: str = "Pending") -> str:
    # Placeholder text for an unset signature/label field in the PDF.
    return value if value else fallback


def _safeVal(value: int | None, fallback: int = 0) -> int:
    # Placeholder count (0) for an unset cell-count field in the PDF.
    return value if value is not None else fallback


def generateResultPdf(result: PatientResultDetail, patientName: str, resultId) -> bytes:
    """Render a one-page A4 PDF lab report for a patient result.

    Args:
        result: the result detail to render (particle counts, notes,
            signatures, etc.).
        patient_name: decrypted patient name to display; passed separately
            since `result` doesn't carry PII.
        result_id: the result's UUID; passed separately since
            `PatientResultDetail` doesn't carry it either (it's a response
            body keyed by the URL's :result_id, not a field of its own).

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
    testDate = result.confirmedAt or result.releasedAt
    infoData = [
        ["Patient Name:", patientName],
        ["Result ID:", str(resultId)],
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
    # result.particleCounts is a list[ParticleCount] (label/count pairs, per
    # PARTICLE_LABELS) — not the "cellCounts" object with named fields
    # (.rbc/.wbc/etc.) this function used to assume; that shape doesn't exist
    # on PatientResultDetail and made every PDF download 500.
    counts = {pc.label: pc.count for pc in result.particleCounts}
    params = [
        ("Red Blood Cells (RBC)", _safeVal(counts.get("erythrocytes"))),
        ("White Blood Cells (WBC)", _safeVal(counts.get("leukocytes"))),
        ("Epithelial Cells", _safeVal(counts.get("epithelial_cells"))),
        ("Casts", _safeVal(counts.get("urinary_casts"))),
        ("Bacteria", _safeVal(counts.get("bacteria"))),
        ("Crystals", _safeVal(counts.get("crystals"))),
        ("Mucus Threads", _safeVal(counts.get("mucus_threads"))),
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
    interpretationText = result.confirmationNotes or "Pending review"
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
                Paragraph(_safeStr(result.analyzedBy), sigLabelStyle),
                # No pathologist-review concept exists in the data model yet —
                # always blank rather than referencing fields that were never
                # real (result.pathologistName/pathologistLicense).
                Paragraph(_safeStr(None), sigLabelStyle),
            ],
            [
                Paragraph("Medical Technologist", sigLabelStyle),
                Paragraph("Pathologist", sigLabelStyle),
            ],
            [
                Paragraph("", sigLabelStyle),
                Paragraph(f"License No: {_safeStr(None)}", sigLabelStyle),
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
