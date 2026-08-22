"""Pydantic request/response models, grouped by domain module. Public API
re-exported below — prefer `from src.urolens.schemas import PatientResponse`
over reaching into a submodule directly (rule 6).

Two pairs of classes are deliberately excluded from this barrel, not
renamed, because each pair is two genuinely different classes that happen
to share a name across sibling modules — re-exporting either would
silently shadow the other:
  - `LabRequestCreateRequest` — defined separately in `lab_request.py`
    (receptionist-facing, has optional physician_id/physician_name fields)
    and `physician.py` (physician-facing, physician is implicit).
  - `ApprovedResultItem` — defined separately in `result_releasing.py`
    (receptionist release queue) and `result_review.py` (supervisor
    approved-today list).
Both stay reachable only via their submodule import, exactly as before
this barrel existed.
"""
from .auth import (  # noqa: F401
    LoginRequest,
    LoginResponse,
    PatientLoginRequest,
    PatientLoginResponse,
)
from .image import AnalysisResultResponse, ImageDiscardResponse  # noqa: F401
from .lab_request import LabRequestCreateResponse, PhysicianItem  # noqa: F401
from .labeling import (  # noqa: F401
    LabelConfirmRequest,
    LabelConfirmResponse,
    LabelPreviewData,
    PrintLabelResponse,
    ReceivedSpecimenSearchItem,
)
from .notifications import NotificationOut, PushTokenRequest  # noqa: F401
from .patient import (  # noqa: F401
    ConsentData,
    PatientCreateRequest,
    PatientResponse,
    SexEnum,
)
from .patient_portal import (  # noqa: F401
    PARTICLE_LABELS,
    ParticleCount,
    PatientResultDetail,
    PatientResultDetailResponse,
    PatientResultItem,
)
from .physician import (  # noqa: F401
    PhysicianPatientItem,
    PhysicianResultDetail,
    PhysicianResultListResponse,
    PhysicianResultSummary,
    SmartDiagnosisDetail,
)
from .queue import (  # noqa: F401
    AssignSpecimenRequest,
    MedTechWorkload,
    MedTechWorkloadItem,
    PendingSpecimenItem,
    QueueAssignRequest,
    QueueAssignResponse,
)
from .result_releasing import (  # noqa: F401
    ApprovedResultsResponse,
    PaginationMeta,
    ReleaseResultRequest,
    ResultReleaseResponse,
)
from .result_review import (  # noqa: F401
    VALID_ESCALATION_PATHS,
    AnnotationRequest,
    AnnotationResponse,
    ApprovedTodayListResponse,
    ApproveRequest,
    ApproveResponse,
    ConfirmResultResponse,
    EscalatedListResponse,
    EscalatedResultItem,
    EscalateRequest,
    EscalateResponse,
    EscalationPath,
    EvidenceMap,
    FullResultDetail,
    ManualOverrideItem,
    OverrideRequest,
    OverrideResponse,
    PendingResultItem,
    PendingResultListResponse,
    ProbabilityLevel,
    ReturnRequest,
    ReturnResponse,
    SmartDiagnosisAttached,
    SmartDiagnosisResponse,
    SmartDiagnosisUnavailable,
    SupervisorStatsResponse,
)
from .specimen import (  # noqa: F401
    LabRequestSearchItem,
    SpecimenListItem,
    SpecimenReceiveRequest,
    SpecimenReceiveResponse,
    SpecimenRejectRequest,
    SpecimenRejectResponse,
)
from .sync import SyncChanges, SyncPullResponse, TableChanges  # noqa: F401
