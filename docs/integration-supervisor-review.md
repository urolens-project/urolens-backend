# Supervisor Review, Annotate, Correct — Frontend Integration Report

UROLENS-150. Covers the result-detail view, spatial annotation/review notes, and
manual override endpoints. Same detail/annotate view backs both the Supervisor's
review workspace and the MedTech's pre-confirmation screen — role differences are
called out per field/row below.

## Endpoints
| Method | Path | Role(s) | Purpose |
|---|---|---|---|
| GET | /api/v1/results/{result_id} | MEDTECH, SUPERVISOR | Full result detail: patient info, image, AI findings, Smart Diagnosis, override history, annotation |
| PATCH | /api/v1/results/{result_id}/annotate | MEDTECH, SUPERVISOR | Save the caller's spatial annotations + free-text review notes |
| POST | /api/v1/results/{id}/override | MEDTECH, SUPERVISOR | Correct a single AI-detected particle count |

401/403 body (any endpoint above): `401 {"error":{"code":"UNAUTHORIZED","message":"Authentication required."}}`
(no/invalid/expired token) · `403 {"error":{"code":"FORBIDDEN","message":"Insufficient permissions."}}` (role isn't MEDTECH or SUPERVISOR)

## Request payload
### PATCH /api/v1/results/{result_id}/annotate — `AnnotationRequest`
| Field | Type | Required | Constraints |
|---|---|---|---|
| annotationNotes | string | yes | no blank/length validation — `""` is accepted and clears the notes |
| spatialAnnotations | `SpatialAnnotationItem[]` \| null | no | omit to leave the previously-saved list untouched; send `[]` to clear it; sending any list **replaces the whole list** |

`SpatialAnnotationItem`:
| Field | Type | Required | Constraints |
|---|---|---|---|
| id | string | yes | 1–64 chars, caller-supplied. Stable across saves — this is how the frontend removes/adjusts one annotation: resend the full list without that `id`, or with its `x`/`y`/`particleType` changed |
| x | number | yes | >= 0 |
| y | number | yes | >= 0 |
| particleType | string | yes | must be one of: `bacteria`, `crystals`, `epithelial_cells`, `erythrocytes`, `leukocytes`, `mucus_threads`, `sperm_cells`, `trichomonas_vaginalis`, `urinary_casts`, `yeast` |

Drift: the UAC describes "areas of interest" (implying a region/shape); the schema only
models a point + particle type — no width/height/shape field exists yet.

### POST /api/v1/results/{id}/override — `OverrideRequest`
| Field | Type | Required | Constraints |
|---|---|---|---|
| parameter | string | yes | 1–100 chars, non-blank after trim. Also accepted as `parameterName` (web's case-bridge sends this) — `parameter` wins if both sent |
| correctedValue | integer | yes | 0–300 inclusive. A fraction or non-finite value is rejected |
| rationale | string | yes | 1–2000 chars, non-blank after trim |
| originalAiValue | number | no | **accepted but ignored** — server always re-derives the original from stored `aiFindings`, never trusts this value |

## Response payload
### GET /api/v1/results/{result_id} — `FullResultDetail` (200)
| Field | Type | Notes |
|---|---|---|
| resultId, specimenId | UUID | |
| sampleUid | string \| null | human-facing sample ID (e.g. `SMP-20260928-00012`); `null` if the specimen wasn't found or has none yet |
| patientUid | string | `""` if the specimen wasn't found |
| patientName | string \| null | **`null` for a MedTech caller** — identifies patient by code only (UROLENS-226); populated (decrypted) for a Supervisor |
| patientAge, patientSex | int \| null, string \| null | |
| medtechName | string | assigned MedTech's username, `""` if unassigned |
| confirmedAt | datetime \| null | |
| confirmationNotes | string \| null | **always `null`** — no backing column exists yet (schema drift, not implemented); kept for API-contract compatibility only |
| aiFindings, flaggedAnomalies, particleClasses | object | raw AI engine output / current effective counts |
| modelVersion | string | |
| manualOverrides | `ManualOverrideItem[]` | full history, oldest first |
| imageUrl | string \| null | short-lived signed URL (private bucket), not a permanent link |
| smartDiagnosis | object \| null | `null` when unavailable |
| smartDiagnosisUnavailable | bool | | 
| status | string | |
| returnReason | string \| null | only set while `status` is `RETURNED_FOR_CORRECTION` |
| annotations | `AnnotationItem[]` | every reviewer's annotation, oldest first (see below) |

`AnnotationItem` (one per reviewer — a MedTech's and a Supervisor's annotations on
the same result are independent rows, both returned):
| Field | Type | Notes |
|---|---|---|
| reviewedBy | UUID | |
| reviewerRole | string | resolved from `reviewedBy` via a `User` lookup; `""` if the user record can't be found |
| annotationNotes | string \| null | |
| spatialAnnotations | `SpatialAnnotationItem[]` \| null | |
| updatedAt | datetime | |

`ManualOverrideItem`:
| Field | Type | Notes |
|---|---|---|
| overrideId | UUID | |
| parameterName | string | |
| originalAiValue, correctedValue | string | numeric values as strings (e.g. `"7.0"`) |
| rationale | string | |
| overriddenAt | datetime | |
| overriddenBy | UUID | who made this correction |
| overriddenByName | string | resolved username; `""` if the user record can't be found |

### PATCH /api/v1/results/{result_id}/annotate — `AnnotationResponse` (200)
| Field | Type | Notes |
|---|---|---|
| resultId | UUID | |
| annotationNotes | string | echoes the request value |
| spatialAnnotations | `SpatialAnnotationItem[]` \| null | **echoes the request parameter, not the persisted value** — if the request omitted `spatialAnnotations` (to update notes only), this response field is `null` even though the previously-saved list is left intact in the database. Re-fetch via `GET /{result_id}` to see the actual current list. |

### POST /api/v1/results/{id}/override — `OverrideResponse` (200)
| Field | Type | Notes |
|---|---|---|
| id, resultId | UUID | |
| parameter | string | always the canonical name, even if the request sent `parameterName` |
| originalAiValue, correctedValue | number | |
| rationale | string | |
| overriddenBy | UUID | the caller |
| overriddenAt | datetime | |

## Error responses
### GET /api/v1/results/{result_id}
| Status | Code | Shape | When |
|---|---|---|---|
| 404 | RESULT_NOT_FOUND | `{"error":{"code":"RESULT_NOT_FOUND","message":"Analysis result not found."}}` | `result_id` doesn't exist |
| 403 | SPECIMEN_NOT_ASSIGNED | `{"error":{"code":"SPECIMEN_NOT_ASSIGNED","message":"Specimen is not assigned to you."}}` | MedTech caller, specimen missing or assigned to a different MedTech |

### PATCH /api/v1/results/{result_id}/annotate
| Status | Code | Shape | When |
|---|---|---|---|
| 422 | VALIDATION_ERROR | `{"error":{"code":"VALIDATION_ERROR","message":"Request validation failed.","details":{"spatialAnnotations.0.particleType":"..."}}}` | an item's `particleType` isn't in the canonical list, or `id`/`x`/`y` fail their constraints |
| 404 | RESULT_NOT_FOUND | `{"error":{"code":"RESULT_NOT_FOUND","message":"..."}}` | `result_id` doesn't exist |
| 403 | SPECIMEN_NOT_ASSIGNED | `{"error":{"code":"SPECIMEN_NOT_ASSIGNED","message":"..."}}` | MedTech, another MedTech's (or unassigned) specimen |
| 422 | RESULT_ALREADY_FINALISED | `{"error":{"code":"RESULT_ALREADY_FINALISED","message":"This result has been finalised and can't be changed."}}` | result is `APPROVED` or `RELEASED` |
| 409 | RESULT_NOT_EDITABLE | `{"error":{"code":"RESULT_NOT_EDITABLE","message":"This result can't be changed in its current status."}}` | MedTech: not `PENDING_CONFIRM`/`RETURNED_FOR_CORRECTION`. Supervisor: not `PENDING_SUPERVISOR_APPROVAL` |

### POST /api/v1/results/{id}/override
| Status | Code | Shape | When |
|---|---|---|---|
| 422 | VALIDATION_ERROR | `{"error":{"code":"VALIDATION_ERROR","message":"Request validation failed.","details":{"rationale":"..."}}}` | `correctedValue` outside 0–300 or non-integer, `rationale`/`parameter` blank or missing |
| 404 | RESULT_NOT_FOUND | `{"error":{"code":"RESULT_NOT_FOUND","message":"No analysis result found with id ..."}}` | `id` doesn't exist |
| 404 | SPECIMEN_NOT_FOUND | `{"error":{"code":"SPECIMEN_NOT_FOUND","message":"..."}}` | MedTech, result's specimen no longer exists |
| 403 | SPECIMEN_NOT_ASSIGNED | `{"error":{"code":"SPECIMEN_NOT_ASSIGNED","message":"Specimen is not assigned to you."}}` | MedTech, another MedTech's specimen |
| 422 | RESULT_ALREADY_FINALISED | `{"error":{"code":"RESULT_ALREADY_FINALISED","message":"This result has been finalised and can't be changed."}}` | result is `APPROVED` or `RELEASED` |
| 409 | RESULT_NOT_EDITABLE | `{"error":{"code":"RESULT_NOT_EDITABLE","message":"This result can't be changed in its current status."}}` | same status rule as annotate |
| 422 | PARAMETER_NOT_FOUND | `{"error":{"code":"PARAMETER_NOT_FOUND","message":"Parameter 'x' not found in AI findings for this result."}}` | `parameter` isn't a key in this result's `aiFindings` |
| 422 | OVERRIDE_UNCHANGED | `{"error":{"code":"OVERRIDE_UNCHANGED","message":"The corrected value is the same as the current value."}}` | `correctedValue` equals the parameter's current effective count (latest override, else the AI value) |

## Auth
Bearer JWT in `Authorization` header. No cookies, no CSRF.

## Sequencing
1. `GET /api/v1/results/{result_id}` → read `manualOverrides`/`annotations` before editing, since `PATCH .../annotate`'s response doesn't reflect an omitted field's persisted value
2. `PATCH /api/v1/results/{result_id}/annotate` and `POST /api/v1/results/{id}/override` are independent — either can be called any number of times while the result is in an editable status

## Examples
### GET /api/v1/results/{result_id}
Response (200):
```json
{
  "resultId": "b3f1c2a0-1111-4a2b-9c3d-000000000010",
  "specimenId": "b3f1c2a0-1111-4a2b-9c3d-000000000011",
  "sampleUid": "SMP-20260928-00012",
  "patientUid": "PAT-000123",
  "patientName": "Maria Santos",
  "patientAge": 34,
  "patientSex": "FEMALE",
  "medtechName": "jdelacruz",
  "confirmedAt": "2026-09-28T09:12:00+08:00",
  "confirmationNotes": null,
  "aiFindings": { "erythrocytes": 7, "leukocytes": 3 },
  "flaggedAnomalies": {},
  "particleClasses": { "erythrocytes": 5, "leukocytes": 3 },
  "modelVersion": "mvp-v1.0",
  "manualOverrides": [
    {
      "overrideId": "b3f1c2a0-1111-4a2b-9c3d-000000000099",
      "parameterName": "erythrocytes",
      "originalAiValue": "7.0",
      "correctedValue": "5.0",
      "rationale": "Recounted, image had overlap",
      "overriddenAt": "2026-09-28T09:30:00+08:00",
      "overriddenBy": "b3f1c2a0-1111-4a2b-9c3d-000000000032",
      "overriddenByName": "asantos"
    }
  ],
  "imageUrl": "https://xyz.supabase.co/storage/v1/object/sign/microscopy/...",
  "smartDiagnosis": null,
  "smartDiagnosisUnavailable": true,
  "status": "PENDING_SUPERVISOR_APPROVAL",
  "returnReason": null,
  "annotations": [
    {
      "reviewedBy": "b3f1c2a0-1111-4a2b-9c3d-000000000041",
      "reviewerRole": "MEDTECH",
      "annotationNotes": "Looks like a cast cluster to me",
      "spatialAnnotations": null,
      "updatedAt": "2026-09-28T09:05:00+08:00"
    },
    {
      "reviewedBy": "b3f1c2a0-1111-4a2b-9c3d-000000000032",
      "reviewerRole": "SUPERVISOR",
      "annotationNotes": "Confirmed, escalating",
      "spatialAnnotations": [
        { "id": "a1", "x": 120, "y": 340, "particleType": "urinary_casts" }
      ],
      "updatedAt": "2026-09-28T09:30:00+08:00"
    }
  ]
}
```

### PATCH /api/v1/results/{result_id}/annotate
Request:
```json
{
  "annotationNotes": "Possible cast cluster, upper-left quadrant",
  "spatialAnnotations": [
    { "id": "a1", "x": 120, "y": 340, "particleType": "urinary_casts" },
    { "id": "a2", "x": 55, "y": 90, "particleType": "crystals" }
  ]
}
```
Response (200):
```json
{
  "resultId": "b3f1c2a0-1111-4a2b-9c3d-000000000010",
  "annotationNotes": "Possible cast cluster, upper-left quadrant",
  "spatialAnnotations": [
    { "id": "a1", "x": 120, "y": 340, "particleType": "urinary_casts" },
    { "id": "a2", "x": 55, "y": 90, "particleType": "crystals" }
  ]
}
```

### POST /api/v1/results/{id}/override
Request:
```json
{
  "parameter": "erythrocytes",
  "correctedValue": 5,
  "rationale": "Recounted, image had overlap"
}
```
Response (200):
```json
{
  "id": "b3f1c2a0-1111-4a2b-9c3d-000000000099",
  "resultId": "b3f1c2a0-1111-4a2b-9c3d-000000000010",
  "parameter": "erythrocytes",
  "originalAiValue": 7,
  "correctedValue": 5,
  "rationale": "Recounted, image had overlap",
  "overriddenBy": "b3f1c2a0-1111-4a2b-9c3d-000000000032",
  "overriddenAt": "2026-09-28T09:30:00+08:00"
}
```
