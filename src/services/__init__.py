"""Business-logic layer: one module per domain service, called from
`api/`/`domains/` routers. Public API re-exported below — prefer
`from src.services import PatientService` over reaching into a
submodule directly (rule 6).

Module-scoped loggers (`log`/`logger`) and internal constants (e.g.
`ai_integration_service.MIN_WIDTH`, `notification_service.EXPO_PUSH_URL`)
are deliberately excluded: several modules define a logger under the same
name, so re-exporting them here would silently collide, and none of them
are meant as public API in the first place — same convention
`models/__init__.py` already follows (only domain classes, nothing
incidental).
"""
from .ai_integration_service import AIIntegrationService  # noqa: F401
from .image_retake_service import ImageRetakeService  # noqa: F401
from .lab_request_service import (  # noqa: F401
    createLabRequest,
    getPhysicians,
    searchPendingLabRequests,
)
from .labeling_service import (  # noqa: F401
    confirmLabelAffixed,
    generateLabel,
    searchReceivedSpecimens,
)
from .manual_override_service import ManualOverrideService  # noqa: F401
from .notification_service import NotificationService  # noqa: F401
from .patient_auth_service import patientLogin, patientLogout  # noqa: F401
from .patient_result_service import PatientResultService  # noqa: F401
from .patient_service import PatientService  # noqa: F401
from .pdf_service import generateResultPdf  # noqa: F401
from .physician_result_service import getResultDetail, listResults  # noqa: F401
from .physician_service import searchPatients  # noqa: F401
from .queue_service import QueueService  # noqa: F401
from .result_confirmation_service import ResultConfirmationService  # noqa: F401
from .result_releasing_service import ResultReleasingService  # noqa: F401
from .result_review_service import (  # noqa: F401
    ResultReviewService,
    getSmartDiagnosis,
)
from .smart_diagnosis_service import SmartDiagnosisService  # noqa: F401
from .specimen_service import (  # noqa: F401
    listSpecimens,
    receiveSpecimen,
    rejectSpecimen,
)
from .sync_service import pull  # noqa: F401
