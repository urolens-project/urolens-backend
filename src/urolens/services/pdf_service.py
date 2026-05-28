from datetime import datetime, timezone

from src.urolens.schemas.patient_portal import PatientResultDetailResponse

_PARTICLE_LABELS = {
    "bacteria":              "Bacteria",
    "crystals":              "Crystals",
    "epithelial-cells":      "Epithelial Cells",
    "epithelial_cells":      "Epithelial Cells",
    "erythrocytes":          "Red Blood Cells (RBC)",
    "leukocytes":            "White Blood Cells (WBC)",
    "mucus-threads":         "Mucus Threads",
    "mucus_threads":         "Mucus Threads",
    "sperm-cells":           "Sperm Cells",
    "sperm_cells":           "Sperm Cells",
    "trichomonas-vaginalis": "Trichomonas Vaginalis",
    "trichomonas_vaginalis": "Trichomonas Vaginalis",
    "urinary-casts":         "Urinary Casts",
    "urinary_casts":         "Urinary Casts",
    "yeast":                 "Yeast",
}


def generate_result_pdf(
    result: PatientResultDetailResponse,
    patient_name: str,
    result_id: str,
) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
        "Title2", parent=styles["Title"], fontSize=16, spaceAfter=8
    )
    subtitle_style = ParagraphStyle(
        "Subtitle2", parent=styles["Normal"], fontSize=10, textColor=colors.grey
    )
    heading_style = ParagraphStyle(
        "Heading2b", parent=styles["Heading2"], fontSize=12, spaceBefore=16, spaceAfter=6
    )
    body_style = ParagraphStyle(
        "Body2", parent=styles["Normal"], fontSize=10, leading=14
    )
    footer_style = ParagraphStyle(
        "Footer2", parent=styles["Normal"], fontSize=7, textColor=colors.grey,
        alignment=TA_CENTER, leading=10,
    )
    sig_style = ParagraphStyle(
        "Sig2", parent=styles["Normal"], fontSize=9, alignment=TA_CENTER, leading=12
    )

    elements = []

    # ── Header ────────────────────────────────────────────────────────────────
    elements.append(Paragraph("UroLens Laboratory", title_style))
    elements.append(Paragraph("Urine Sediment Analysis Report", subtitle_style))
    elements.append(Spacer(1, 6 * mm))
    elements.append(
        Paragraph(
            f"Date Generated: {datetime.now(timezone.utc).strftime('%B %d, %Y')}",
            body_style,
        )
    )
    elements.append(Spacer(1, 6 * mm))

    # ── Patient Information ───────────────────────────────────────────────────
    elements.append(Paragraph("Patient Information", heading_style))
    test_date = result.confirmed_at or result.released_at
    info_data = [
        ["Patient Name:", patient_name],
        ["Result ID:", result_id],
        ["Test Type:", result.test_type],
        ["Date of Test:", test_date.strftime("%B %d, %Y") if test_date else "N/A"],
        ["Status:", result.status],
    ]
    info_table = Table(info_data, colWidths=[45 * mm, 100 * mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME",    (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",    (0, 0), (-1, -1), 10),
        ("TEXTCOLOR",   (0, 0), (0, -1),  colors.HexColor("#555555")),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 6 * mm))

    # ── Particle Counts ───────────────────────────────────────────────────────
    elements.append(Paragraph("Particle / Cell Count Results", heading_style))
    counts_data = [["Parameter", "Count"]]
    for pc in result.particle_counts:
        label = _PARTICLE_LABELS.get(pc.label, pc.label.replace("-", " ").replace("_", " ").title())
        counts_data.append([label, str(pc.count)])
    counts_table = Table(counts_data, colWidths=[110 * mm, 40 * mm])
    counts_table.setStyle(TableStyle([
        ("FONTNAME",      (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 0), (-1, -1), 10),
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2c3e50")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("ALIGN",         (1, 0), (1, -1),  "CENTER"),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f6fa")]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(counts_table)
    elements.append(Spacer(1, 6 * mm))

    # ── Interpretation / Notes ────────────────────────────────────────────────
    elements.append(Paragraph("Interpretation / Notes", heading_style))
    interp = result.confirmation_notes or "No additional notes."
    elements.append(Paragraph(interp, body_style))
    elements.append(Spacer(1, 10 * mm))

    # ── Signature ─────────────────────────────────────────────────────────────
    medtech = result.analyzed_by or "Medical Technologist"
    sig_table = Table(
        [
            [Paragraph("Examined by:", sig_style), Paragraph("", sig_style)],
            [Paragraph("______________________", sig_style), Paragraph("", sig_style)],
            [Paragraph(medtech, sig_style), Paragraph("", sig_style)],
            [Paragraph("Medical Technologist", sig_style), Paragraph("", sig_style)],
        ],
        colWidths=[85 * mm, 65 * mm],
    )
    sig_table.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(sig_table)
    elements.append(Spacer(1, 10 * mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    elements.append(Spacer(1, 8 * mm))
    elements.append(Paragraph(
        "This report is generated by UroLens. For clinical decisions, consult your physician.",
        footer_style,
    ))
    elements.append(Paragraph("CONFIDENTIAL — For patient use only", footer_style))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
