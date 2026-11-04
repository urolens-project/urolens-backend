"""Unit tests — multi-field error codes render in the standard error envelope."""
from __future__ import annotations

import json
import uuid

import pytest

from main import httpExceptionHandler
from src.core import exceptions as ex


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (ex.ImageNotMicroscopyError(), 422, "IMAGE_NOT_MICROSCOPY"),
        (ex.ImageExposureError(), 422, "IMAGE_EXPOSURE"),
        (ex.AppUpdateRequiredError(), 410, "APP_UPDATE_REQUIRED"),
        (ex.FieldNotOpenedError(), 409, "FIELD_NOT_OPENED"),
        (ex.FieldVersionConflictError(), 409, "FIELD_VERSION_CONFLICT"),
        (ex.FieldsUndecidedError([(uuid.uuid4(), 1), (uuid.uuid4(), 2)]), 409, "FIELDS_UNDECIDED"),
        (ex.FieldsBelowMinimumError(4, 10), 409, "FIELDS_BELOW_MINIMUM"),
        (ex.FieldsNotVerifiedError(), 409, "FIELDS_NOT_VERIFIED"),
        (ex.FeatureDisabledError(), 404, "FEATURE_DISABLED"),
        (ex.NotImplementedYetError(), 501, "NOT_IMPLEMENTED"),
    ],
)
@pytest.mark.asyncio
async def test_codeRendersInTheEnvelope(exc, status, code):
    response = await httpExceptionHandler(None, exc)
    assert response.status_code == status
    body = json.loads(response.body)
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_updateMessageIsTheOneTheAppShows():
    assert ex.AppUpdateRequiredError().detail == "Update UroLens to continue"


FIELD_A = uuid.UUID("00000000-0000-0000-0000-0000000009a1")
FIELD_B = uuid.UUID("00000000-0000-0000-0000-0000000009b2")


def test_undecidedMessageNamesTheCount():
    assert ex.FieldsUndecidedError([(FIELD_A, 3), (FIELD_B, 7)]).detail == "2 fields not reviewed."
    assert ex.FieldsUndecidedError([(FIELD_A, 3)]).detail == "1 field not reviewed."


@pytest.mark.asyncio
async def test_undecidedFieldsAreNamedInTheEnvelopeDetails():
    response = await httpExceptionHandler(None, ex.FieldsUndecidedError([(FIELD_A, 3), (FIELD_B, 7)]))

    assert json.loads(response.body) == {"error": {
        "code": "FIELDS_UNDECIDED",
        "message": "2 fields not reviewed.",
        "details": {"undecidedFields": [
            {"fieldId": str(FIELD_A), "fieldNo": 3},
            {"fieldId": str(FIELD_B), "fieldNo": 7},
        ]},
    }}


@pytest.mark.asyncio
async def test_anErrorWithoutDetailsHasNoDetailsKey():
    response = await httpExceptionHandler(None, ex.FieldsBelowMinimumError(4, 10))

    assert set(json.loads(response.body)["error"]) == {"code", "message"}


def test_belowMinimumMessageNamesBothNumbers():
    assert ex.FieldsBelowMinimumError(4, 10).detail == "4 of 10 required fields accepted."
