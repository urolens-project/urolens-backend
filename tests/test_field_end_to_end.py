"""End-to-end test of the multi-field web lane (mocked database, flag on).

Ten accepted fields are opened, one has its boxes corrected, another gets an override,
all are verified and the result confirmed; a supervisor returns it pointing at a field
(which clears the marks); the fields are verified again and the result re-confirmed; the
supervisor approves; and the patient's detail and PDF show the averages, "<1" for a rare
class, and the label with the number of fields.

One in-memory "world" stands in for the database, so the real services run end to end:
`ResultFieldService`, `FieldCorrectionService`, `ManualOverrideService`,
`ResultConfirmationService`, `ResultReviewService`, `PatientResultService` and the PDF.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import settings
from src.models.analysis_result import AnalysisResult, ResultStatus
from src.models.field_annotation_edit import FieldAnnotationEdit
from src.models.image import Image, ImageStatus
from src.models.manual_override import ManualOverride
from src.models.result_confirmation import ResultConfirmation
from src.models.result_field import FieldStatus, ResultField
from src.models.result_return import ResultReturn
from src.models.specimen import Specimen
from src.schemas.field import SaveAnnotationsRequest
from src.schemas.patient_portal import PatientResultDetail
from src.services import pdf_service
from src.services.field_correction_service import FieldCorrectionService
from src.services.manual_override_service import ManualOverrideService
from src.services.notification_service import NotificationService
from src.services.patient_result_service import PatientResultService
from src.services.result_confirmation_service import ResultConfirmationService
from src.services.result_field_service import ResultFieldService
from src.services.result_review_service import ResultReviewService
from src.services.smart_diagnosis_service import SmartDiagnosisService

RESULT_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a1")
SPECIMEN_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a2")
MEDTECH_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a3")
SUPERVISOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a4")
PATIENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a5")
PATIENT_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000020a6")
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)

MEDTECH = {"user_id": str(MEDTECH_ID), "role": "MEDTECH"}
AI_RBC = (3, 5, 2, 9, 4, 4, 1, 6, 3, 4)
CASTS = (0, 0, 0, 0, 0, 0, 1, 1, 1, 0)           # seen in 3 of 10 fields
DETECTIONS_FIELD_4 = [
    {"id": f"d{n}", "particleType": "erythrocytes", "confidence": 0.9,
     "x": float(n), "y": 10.0, "w": 5.0, "h": 5.0}
    for n in range(1, 10)
]


class World:
    """An in-memory stand-in for the database, answering the services' statements by their SQL."""

    def __init__(self):
        self.specimen = Specimen(
            specimenId=SPECIMEN_ID, medtechId=MEDTECH_ID, status="PROCESSING", sampleUid="SMP-20261027-00009",
            patientUid="PAT-0009", labRequestId=None)
        self.result = AnalysisResult(
            resultId=RESULT_ID, specimenId=SPECIMEN_ID, status=ResultStatus.PENDING_CONFIRM, patientId=PATIENT_ID,
            aiFindings={}, particleClasses={}, fieldCount=0, fieldSummary=None, isLegacySingleField=False,
            modelVersion="mvp-v1.0", smartDiagnosisUnavailable=False)
        self.result.manualOverrides = []
        self.fields: dict[uuid.UUID, ResultField] = {}
        self.images: dict[uuid.UUID, Image] = {}
        self.edits: list[FieldAnnotationEdit] = []
        self.overrides: list[ManualOverride] = []
        self.returns: list[ResultReturn] = []
        self.confirmation: ResultConfirmation | None = None
        self.statements: list[str] = []
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        for n in range(1, 11):
            ai = {"erythrocytes": AI_RBC[n - 1], "urinary_casts": CASTS[n - 1]}
            field = ResultField(
                fieldId=uuid.UUID(f"00000000-0000-0000-0000-0000000021{n:02d}"), resultId=RESULT_ID,
                specimenId=SPECIMEN_ID, imageId=uuid.UUID(f"00000000-0000-0000-0000-0000000022{n:02d}"),
                fieldNo=n, magnification="HPF", status=FieldStatus.ACCEPTED, aiFindings=ai,
                detections=[dict(d) for d in DETECTIONS_FIELD_4] if n == 4 else [],
                imageWidth=800, imageHeight=600, confidenceScores={"erythrocytes": 0.9},
                effectiveCounts=dict(ai), smartDiagnosis=None, smartDiagnosisUnavailable=False,
                modelVersion="mvp-v1.0", version=1, createdAt=LONG_AGO, updatedAt=LONG_AGO)
            self.fields[field.fieldId] = field
            self.images[field.imageId] = Image(
                imageId=field.imageId, specimenId=SPECIMEN_ID, storageKey=f"key-{n}", status=ImageStatus.ACTIVE)

    # helpers -------------------------------------------------------------
    @property
    def ordered(self) -> list[ResultField]:
        return sorted(self.fields.values(), key=lambda f: f.fieldNo)

    def _params(self, stmt) -> list:
        values: list = []
        for value in stmt.compile().params.values():
            values.extend(value if isinstance(value, (list, tuple)) else [value])
        return values

    def _field(self, stmt) -> ResultField | None:
        return next((self.fields[v] for v in self._params(stmt) if isinstance(v, uuid.UUID) and v in self.fields), None)

    # session API ---------------------------------------------------------
    async def get(self, model, key, **kwargs):
        if model is AnalysisResult:
            return self.result
        if model is Specimen:
            return self.specimen
        if model is Image:
            return self.images.get(key)
        return None

    async def refresh(self, obj, **kwargs):
        if isinstance(obj, AnalysisResult) and obj.updatedAt is None:
            obj.updatedAt = datetime.now(UTC)

    def add(self, obj):
        if isinstance(obj, FieldAnnotationEdit):
            self.edits.append(obj)
        elif isinstance(obj, ManualOverride):
            self.overrides.append(obj)
        elif isinstance(obj, ResultReturn):
            self.returns.append(obj)
        elif isinstance(obj, ResultConfirmation):
            self.confirmation = obj

    async def execute(self, stmt):
        sql = str(stmt)
        self.statements.append(sql)
        low = " ".join(sql.lower().split())
        out = MagicMock()
        field = self._field(stmt)
        if low.startswith("select result_fields.specimen_id from"):
            out.scalar_one_or_none.return_value = field.specimenId if field else None
        elif low.startswith("select result_fields.result_id from"):
            out.scalar_one_or_none.return_value = field.resultId if field else None
        elif low.startswith("select result_fields.image_id from"):
            out.scalars.return_value.all.return_value = [
                f.imageId for f in self.ordered if f.status == FieldStatus.ACCEPTED]
        elif "from result_fields join images" in low:
            out.all.return_value = [(f, self.images[f.imageId].storageKey) for f in self.ordered]
        elif "from result_fields" in low and "result_fields.status =" in low:
            out.scalars.return_value.all.return_value = [f for f in self.ordered if f.status == FieldStatus.ACCEPTED]
        elif low.startswith("update result_fields"):
            params = stmt.compile().params
            if "last_opened_by" in params:
                field.lastOpenedBy, field.lastOpenedAt = params["last_opened_by"], params["last_opened_at"]
                out.rowcount = 1
            else:
                marked = [f for f in self.fields.values() if f.verifiedBy is not None]
                for f in marked:
                    f.verifiedBy = f.verifiedAt = None
                out.rowcount = len(marked)
        elif "from result_fields" in low:
            out.scalar_one_or_none.return_value = field
        elif low.startswith("select analysis_results.status from"):
            out.scalar_one.return_value = self.result.status
        elif low.startswith("select analysis_results.result_id"):
            out.scalar_one.return_value = self.result
            out.scalar_one_or_none.return_value = self.result
        elif low.startswith("update analysis_results"):
            self.result.status = stmt.compile().params["status"]
            out.rowcount = 1
        elif low.startswith("select images.storage_key"):
            out.scalar_one_or_none.return_value = self.images[field.imageId].storageKey if field else "key"
        elif low.startswith("update field_annotation_edits"):
            for edit in self.edits:
                if edit.supersededAt is None:
                    edit.supersededAt = datetime.now(UTC)
        elif "from field_annotation_edits" in low and "limit" in low:
            current = [e for e in self.edits if e.supersededAt is None and e.fieldId == (field.fieldId if field else None)]
            out.first.return_value = (current[0].editId,) if current else None
        elif "from field_annotation_edits" in low:
            wanted = {v for v in self._params(stmt) if isinstance(v, uuid.UUID)}
            out.scalars.return_value.all.return_value = [
                e for e in self.edits if e.supersededAt is None and e.fieldId in wanted]
            out.scalars.return_value.distinct = out.scalars.return_value.all
        elif "from manual_overrides" in low:
            wanted = {v for v in self._params(stmt) if isinstance(v, uuid.UUID)}
            out.scalars.return_value.all.return_value = sorted(
                (o for o in self.overrides if o.fieldId in wanted), key=lambda o: o.overriddenAt)
        elif "from result_confirmations" in low:
            out.scalar_one_or_none.return_value = self.confirmation
        elif "count(*)" in low and "result_returns" in low:
            since = self.confirmation.confirmedAt if self.confirmation else None
            out.scalar_one.return_value = sum(1 for r in self.returns if since is not None and r.returnedAt > since)
        elif "from patients" in low:
            out.scalar_one_or_none.return_value = PATIENT_ID
        else:  # pragma: no cover - a query the world does not know about
            raise AssertionError(f"unexpected statement: {low}")
        return out


class Recorder:
    """What the audit logger was told, in order."""

    def __init__(self):
        self.record = AsyncMock()

    @property
    def events(self) -> list[str]:
        return [call.kwargs["eventType"] for call in self.record.call_args_list]

    def details(self, eventType: str) -> list[dict]:
        return [c.kwargs.get("detailJson") or {} for c in self.record.call_args_list
                if c.kwargs["eventType"] == eventType]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    monkeypatch.setattr(settings, "minFieldsPerSpecimen", 10)
    with patch("src.services.result_field_service.signedImageUrls", AsyncMock(return_value={})), \
         patch("src.services.result_confirmation_service.requireProcessingConsent", AsyncMock()), \
         patch("src.services.result_confirmation_service.loadNormalMax", return_value={}), \
         patch("src.services.result_field_service.loadNormalMax", return_value={}):
        yield World()


def request():
    req = MagicMock()
    req.client = MagicMock(host="127.0.0.1")
    return req


@pytest.mark.asyncio
async def test_fromTheFirstOpenToThePatientsPdf(world):
    audit = Recorder()
    notif = MagicMock(spec=NotificationService)
    notif.notifySupervisorResultReady = AsyncMock()
    notif.notifyMedtechResultReturned = AsyncMock()
    diag = MagicMock(spec=SmartDiagnosisService)
    diag.run = AsyncMock(return_value=None)

    fieldService = ResultFieldService(db=world, auditLogger=audit, _notifService=notif)
    corrections = FieldCorrectionService(db=world, auditLogger=audit)
    overrides = ManualOverrideService(db=world, auditLogger=audit)
    confirmation = ResultConfirmationService(
        db=world, auditLogger=audit, _smartDiagnosisService=diag, _notifService=notif)
    review = ResultReviewService(db=world, auditLogger=audit, _notifService=notif)
    patientService = PatientResultService(db=world, auditLogger=audit)
    fields = world.ordered
    field2, field4 = fields[1], fields[3]

    # 1. the MedTech opens every field
    for field in fields:
        detail = await fieldService.getField(field.fieldId, MEDTECH, request())
        assert field.lastOpenedBy == MEDTECH_ID and field.lastOpenedAt is not None
        assert detail.fieldNo == field.fieldNo
    assert audit.events == []                                        # opens are not audited

    # 2. boxes corrected on field 4: three of nine red cells were crystals
    await corrections.saveAnnotations(
        field4.fieldId, MEDTECH_ID,
        SaveAnnotationsRequest(removedDetectionIds=["d1", "d2", "d3"], addedBoxes=[], expectedVersion=1), request())
    assert field4.effectiveCounts["erythrocytes"] == 6 and field4.version == 2

    # 3. an override on field 2: five becomes three
    await overrides.overrideFieldParameter(
        field2.fieldId, "erythrocytes", 3, "Recounted by eye", MEDTECH_ID, request())
    assert field2.effectiveCounts["erythrocytes"] == 3 and field2.aiFindings["erythrocytes"] == 5

    # 4. every field is verified (the corrected two count as opened by their correction)
    for field in fields:
        await corrections.markVerified(field.fieldId, MEDTECH_ID, request())
    assert all(f.verifiedBy == MEDTECH_ID for f in fields)

    # 5. the summary agrees with what confirm is about to store
    with patch("urolens_ai.generate_smart_diagnosis", MagicMock(side_effect=RuntimeError("decimals"))):
        summary = await fieldService.getVerificationSummary(RESULT_ID, MEDTECH)
    assert summary.canConfirm is True and summary.smartDiagnosisUnavailable is True
    assert summary.perClass["erythrocytes"].mean == 3.6 and summary.perClass["urinary_casts"].displayValue == "<1"
    assert {(c.fieldNo, c.kind) for c in summary.corrections} == {(2, "OVERRIDE"), (4, "BOXES")}

    # 6. confirm
    first = await confirmation.confirmResult(RESULT_ID, MEDTECH_ID, request())
    assert first.resubmitted is False and world.result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    stored = world.result.fieldSummary
    assert stored["perClass"]["erythrocytes"]["mean"] == 3.6                 # exact
    assert stored["perClass"]["urinary_casts"]["mean"] == 0.3
    assert world.result.particleClasses == {"erythrocytes": 4, "urinary_casts": 1}
    assert world.result.aiFindings["erythrocytes"] == 4.1 and world.result.fieldCount == 10
    assert diag.run.await_args.kwargs["counts"] == {"erythrocytes": 3.6, "urinary_casts": 0.3}
    summaryNumbers = {p: (a.mean, a.min, a.max, a.total) for p, a in summary.perClass.items()}
    assert summaryNumbers == {p: (a["mean"], a["min"], a["max"], a["total"]) for p, a in stored["perClass"].items()}
    world.confirmation.confirmedAt = LONG_AGO                                # so the return below is later

    # 7. the supervisor returns it, pointing at field 4: every verified mark is cleared
    returned = await review.returnResult(
        RESULT_ID, SUPERVISOR_ID, "Look at the red cells again", request(),
        fieldId=field4.fieldId, particleType="erythrocytes")
    assert world.result.status == ResultStatus.RETURNED_FOR_CORRECTION
    assert (returned["fieldId"], returned["particleType"]) == (field4.fieldId, "erythrocytes")
    assert all(f.verifiedBy is None and f.verifiedAt is None for f in fields)
    (storedReturn,) = world.returns
    assert (storedReturn.reason, storedReturn.fieldId, storedReturn.particleType) == (
        "Look at the red cells again", field4.fieldId, "erythrocytes")

    # 8. verified again, then re-confirmed: a resubmission
    for field in fields:
        await corrections.markVerified(field.fieldId, MEDTECH_ID, request())
    second = await confirmation.confirmResult(RESULT_ID, MEDTECH_ID, request())
    assert second.resubmitted is True and world.result.status == ResultStatus.PENDING_SUPERVISOR_APPROVAL
    assert world.result.fieldCount == 10

    # 9. approved, then released (the receptionist's step, simulated)
    await review.approveResult(RESULT_ID, SUPERVISOR_ID, "Fine", request())
    assert world.result.status == ResultStatus.APPROVED and world.specimen.status == "COMPLETED"
    world.result.status = "RELEASED"
    world.result.releasedAt = datetime.now(UTC)

    # 10. the patient's detail and PDF
    detail = await patientService.getResultDetail(RESULT_ID, PATIENT_USER_ID, request())
    byLabel = {p.label: p for p in detail.particleCounts}
    assert (detail.fieldCount, detail.countsLabel) == (10, "average per field (10 fields)")
    assert (byLabel["erythrocytes"].count, byLabel["erythrocytes"].displayValue) == (4, "4")
    assert (byLabel["urinary_casts"].count, byLabel["urinary_casts"].displayValue) == (1, "<1")
    assert byLabel["bacteria"].displayValue == "0"

    paragraphs: list[str] = []
    rows: list = []
    with patch("reportlab.platypus.Paragraph", side_effect=lambda text, *a, **k: paragraphs.append(text) or MagicMock()), \
         patch("reportlab.platypus.Table", side_effect=lambda data, *a, **k: rows.extend(data) or MagicMock()), \
         patch("reportlab.platypus.SimpleDocTemplate.build", return_value=None):
        pdf_service.generateResultPdf(PatientResultDetail(**detail.model_dump()), "Jane Doe", RESULT_ID)
    assert "Counts shown as: average per field (10 fields)" in paragraphs
    assert next(r for r in rows if "Red Blood Cells (RBC)" in str(r))[1] == "4"
    assert next(r for r in rows if "Casts" in str(r))[1] == "<1"

    # 11. the audit trail: every state change, in order
    assert audit.events == (
        ["FIELD_ANNOTATIONS_SAVED", "RESULT_OVERRIDDEN"]
        + ["FIELD_VERIFIED"] * 10
        + ["RESULT_CONFIRMED", "RESULT_RETURNED"]
        + ["FIELD_VERIFIED"] * 10
        + ["RESULT_RESUBMITTED", "RESULT_APPROVED", "RESULT_VIEWED"]
    )
    assert audit.details("RESULT_CONFIRMED") == [{"specimen_id": str(SPECIMEN_ID), "field_count": 10}]
    assert audit.details("RESULT_RESUBMITTED") == [{"specimen_id": str(SPECIMEN_ID), "field_count": 10}]
    assert audit.details("RESULT_RETURNED") == [{
        "specimen_id": str(SPECIMEN_ID), "fields_unverified": 10, "field_id": str(field4.fieldId)}]
    assert audit.details("FIELD_ANNOTATIONS_SAVED") == [{
        "specimen_id": str(SPECIMEN_ID), "field_id": str(field4.fieldId), "added": 0, "removed": 3}]

    # ids and counts only: no free text, no coordinates, anywhere
    everything = json.dumps([c.kwargs for c in audit.record.call_args_list], default=str)
    for text in ("Look at the red cells again", "Recounted by eye", "Fine"):
        assert text not in everything
    forbidden = {"reason", "rationale", "notes", "x", "y", "w", "h", "coordinates", "boxes"}
    for call in audit.record.call_args_list:
        assert not forbidden & set(call.kwargs.get("detailJson") or {})
    assert {c.kwargs["userId"] for c in audit.record.call_args_list} <= {MEDTECH_ID, SUPERVISOR_ID, PATIENT_USER_ID}

    # the AI output was never edited
    assert field4.detections == DETECTIONS_FIELD_4 and field4.aiFindings["erythrocytes"] == 9
    assert all(f.aiFindings["erythrocytes"] == AI_RBC[f.fieldNo - 1] for f in fields)
    assert notif.notifyMedtechResultReturned.await_count == 1 and notif.notifySupervisorResultReady.await_count == 2
