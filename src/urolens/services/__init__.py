"""Business-logic layer: one module per domain service, called from
`api/`/`domains/` routers. Public API re-exported below — prefer
`from src.urolens.services import PatientService` over reaching into a
submodule directly (rule 6).

Module-scoped loggers (`log`/`logger`) and internal constants (e.g.
`ai_integration_service.MIN_WIDTH`, `notification_service.EXPO_PUSH_URL`)
are deliberately excluded: several modules define a logger under the same
name, so re-exporting them here would silently collide, and none of them
are meant as public API in the first place — same convention
`models/__init__.py` already follows (only domain classes, nothing
incidental)."""
from .ai_integration_service import AIIntegrationService  # noqa: F401
from .image_retake_service import ImageRetakeService  # noqa: F401
from .lab_request_service import (  # noqa: F401
    create_lab_request,
    get_physicians,
    search_pending_lab_requests,
)
from .labeling_service import (  # noqa: F401
    confirm_label_affixed,
    generate_label,
    search_received_specimens,
)
from .manual_override_service import ManualOverrideService  # noqa: F401
from .notification_service import NotificationService  # noqa: F401
from .patient_auth_service import patient_login, patient_logout  # noqa: F401
from .patient_result_service import PatientResultService  # noqa: F401
from .patient_service import PatientService  # noqa: F401
from .pdf_service import generate_result_pdf  # noqa: F401
from .physician_result_service import get_result_detail, list_results  # noqa: F401
from .physician_service import search_patients  # noqa: F401
from .queue_service import QueueService  # noqa: F401
from .result_confirmation_service import ResultConfirmationService  # noqa: F401
from .result_releasing_service import ResultReleasingService  # noqa: F401
from .result_review_service import ResultReviewService, get_smart_diagnosis  # noqa: F401
from .smart_diagnosis_service import SmartDiagnosisService  # noqa: F401
from .specimen_service import (  # noqa: F401
    list_specimens,
    receive_specimen,
    reject_specimen,
)
from .sync_service import pull  # noqa: F401
