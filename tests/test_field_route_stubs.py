"""Unit tests — the guards every multi-field route shares (`api/fields.py`).

Every field route is built now; this file keeps the checks they share. Flag
off: 404 FEATURE_DISABLED. Role checks come first, and the flag check beats a
body-validation 422. Behaviour is tested with each feature (test_field_read_paths.py,
test_field_corrections.py, test_field_confirm.py, test_field_capture_decisions.py).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app
from src.core.config import settings
from src.core.rbac import RequireRole, getCurrentUser

ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
MEDTECH = ["MEDTECH"]
BOTH = ["MEDTECH", "SUPERVISOR"]

ANNOTATIONS = {"removedDetectionIds": [], "addedBoxes": [], "expectedVersion": 1}
OVERRIDE = {"particleType": "crystals", "correctedValue": 3, "rationale": "miscounted"}
REVERT = {"particleType": "crystals", "rationale": "box count was right"}

# (method, path, allowed roles, valid body or None)
ROUTES = [
    ("POST", f"/api/v1/fields/{ID}/accept", MEDTECH, None),
    ("POST", f"/api/v1/fields/{ID}/discard", MEDTECH, None),
    ("POST", f"/api/v1/results/{ID}/fields/submit", MEDTECH, None),
    ("GET", f"/api/v1/results/{ID}/fields", BOTH, None),
    ("GET", f"/api/v1/fields/{ID}", BOTH, None),
    ("PUT", f"/api/v1/fields/{ID}/annotations", MEDTECH, ANNOTATIONS),
    ("POST", f"/api/v1/fields/{ID}/override", MEDTECH, OVERRIDE),
    ("POST", f"/api/v1/fields/{ID}/override/revert", MEDTECH, REVERT),
    ("POST", f"/api/v1/fields/{ID}/verify", MEDTECH, None),
    ("POST", f"/api/v1/results/{ID}/reopen-capture", MEDTECH, None),
    ("GET", f"/api/v1/results/{ID}/summary", BOTH, None),
]
BODY_ROUTES = [r for r in ROUTES if r[3] is not None]

# Built by a lane: no longer a stub, still behind the role and flag checks.
BUILT_ROUTES = [
    ("POST", "/api/v1/fields", MEDTECH, None),  # capture a field (multipart)
]


def _loginAs(role: str) -> None:
    app.dependency_overrides[getCurrentUser] = lambda: {"user_id": str(ID), "role": role}


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.pop(getCurrentUser, None)


def _call(client: TestClient, method: str, path: str, body):
    return client.request(method, path, json=body)


@pytest.mark.parametrize(("method", "path", "roles", "body"), ROUTES + BUILT_ROUTES)
def test_flagOffAnswers404(client, monkeypatch, method, path, roles, body):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    _loginAs(roles[0])
    response = _call(client, method, path, body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FEATURE_DISABLED"


@pytest.mark.parametrize(("method", "path", "roles", "body"), ROUTES + BUILT_ROUTES)
def test_wrongRoleIsForbiddenBeforeTheFlagCheck(client, monkeypatch, method, path, roles, body):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    _loginAs("PATIENT")
    assert _call(client, method, path, body).status_code == 403


def test_supervisorCannotUseMedtechOnlyRoutes(client, monkeypatch):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    _loginAs("SUPERVISOR")
    medtechOnly = [r for r in ROUTES + BUILT_ROUTES if r[2] == MEDTECH]
    assert medtechOnly
    for method, path, _roles, body in medtechOnly:
        assert _call(client, method, path, body).status_code == 403, path


@pytest.mark.parametrize(("method", "path", "roles", "body"), ROUTES + BUILT_ROUTES)
def test_noTokenIsUnauthorized(client, method, path, roles, body):
    response = _call(client, method, path, body)
    assert response.status_code in (401, 403)


@pytest.mark.parametrize(("method", "path", "roles", "body"), BODY_ROUTES)
def test_flagOffBeatsABodyValidationError(client, monkeypatch, method, path, roles, body):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", False)
    _loginAs(roles[0])
    response = _call(client, method, path, {"nonsense": True})
    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path", "roles", "body"), BODY_ROUTES)
def test_flagOnValidatesTheBody(client, monkeypatch, method, path, roles, body):
    monkeypatch.setattr(settings, "fieldWorkflowEnabled", True)
    _loginAs(roles[0])
    assert _call(client, method, path, {"nonsense": True}).status_code == 422


def test_everyFieldRouteHasARoleCheckAndAResponseModel():
    fieldRoutes = [
        r for r in app.routes
        if isinstance(r, APIRoute) and "fields" in (r.tags or [])
    ]
    assert len(fieldRoutes) == len(ROUTES) + len(BUILT_ROUTES)
    for route in fieldRoutes:
        assert route.response_model is not None, route.path
        calls = [d.call for d in route.dependant.dependencies]
        assert any(isinstance(c, RequireRole) for c in calls), route.path
