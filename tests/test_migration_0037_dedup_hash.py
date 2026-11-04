"""Unit tests — migration 0037 (`patients.dedup_hash` backfill).

Runs the migration's `upgrade()` against a stand-in connection:
- encrypted rows and legacy plaintext rows (patients registered before PII
  encryption) both get the hash the application computes for that person;
- a value that is missing or can't be decrypted stops the migration, naming the
  patient UIDs, before any row or constraint is changed;
- two patients with the same name and date of birth still stop it, including
  when one is stored as plaintext and the other encrypted;
- the column is filled before `NOT NULL` and the unique constraint are added.
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.encryption import encryptPii
from src.services.patient_service import PatientService

_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0037_patients_dedup_hash.py"

Row = tuple[uuid.UUID, str, str | None, str | None, str | None]


def _loadMigration() -> object:
    spec = importlib.util.spec_from_file_location("migration_0037", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _encrypted(patientUid: str, firstName: str, lastName: str, dob: str) -> Row:
    return (uuid.uuid4(), patientUid, encryptPii(firstName), encryptPii(lastName), encryptPii(dob))


def _plaintext(patientUid: str, firstName: str | None, lastName: str | None, dob: str | None) -> Row:
    return (uuid.uuid4(), patientUid, firstName, lastName, dob)


class _FakeOp:
    """Records what the migration does, in order, and serves the patient rows.

    `op` is the stand-in for Alembic's `op`: its `execute` and the connection
    from its `get_bind()` both report here.
    """

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.steps: list[str] = []
        self.updates: dict[str, str] = {}
        self.op = MagicMock()
        self.op.execute.side_effect = self._execute
        self.op.get_bind.return_value.execute.side_effect = self._bindExecute

    def _execute(self, sql: str) -> None:
        self.steps.append(" ".join(str(sql).split()))

    def _bindExecute(self, stmt: object, params: dict[str, str] | None = None) -> MagicMock:
        if params is None:
            return MagicMock(fetchall=lambda: self.rows)
        self.steps.append("UPDATE")
        self.updates[params["patientId"]] = params["dedupHash"]
        return MagicMock()


def _upgrade(rows: list[Row]) -> _FakeOp:
    migration = _loadMigration()
    fakeOp = _FakeOp(rows)
    with patch.object(migration, "op", fakeOp.op):
        migration.upgrade()
    return fakeOp


def _appHash(firstName: str, lastName: str, dob: date) -> str:
    return PatientService._computeDedupHash(firstName, lastName, dob)


def test_encryptedAndLegacyPlaintextPatientsGetTheHashTheApplicationComputes() -> None:
    encrypted = _encrypted("PAT-000001", "Maria", "Santos", "1990-04-12")
    legacy = _plaintext("PAT-000002", "Juan", "Dela Cruz", "1985-11-03")

    fakeOp = _upgrade([encrypted, legacy])

    assert fakeOp.updates == {
        str(encrypted[0]): _appHash("Maria", "Santos", date(1990, 4, 12)),
        str(legacy[0]): _appHash("Juan", "Dela Cruz", date(1985, 11, 3)),
    }


def test_theHashIgnoresCaseAndSurroundingSpacesLikeTheApplication() -> None:
    legacy = _plaintext("PAT-000003", "  JUAN ", "dela cruz  ", "1985-11-03")

    fakeOp = _upgrade([legacy])

    assert fakeOp.updates[str(legacy[0])] == _appHash("Juan", "Dela Cruz", date(1985, 11, 3))


def test_theColumnIsFilledBeforeItIsMadeRequiredAndUnique() -> None:
    fakeOp = _upgrade([_encrypted("PAT-000001", "Maria", "Santos", "1990-04-12")])

    assert fakeOp.steps == [
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS dedup_hash VARCHAR(64)",
        "UPDATE",
        "ALTER TABLE patients ALTER COLUMN dedup_hash SET NOT NULL",
        "ALTER TABLE patients ADD CONSTRAINT uq_patients_dedup_hash UNIQUE (dedup_hash)",
    ]


def test_anEmptyTableStillGetsTheColumnAndConstraint() -> None:
    fakeOp = _upgrade([])

    assert fakeOp.updates == {}
    assert fakeOp.steps[-1] == "ALTER TABLE patients ADD CONSTRAINT uq_patients_dedup_hash UNIQUE (dedup_hash)"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (2, None),
        (2, ""),
        (3, None),
        (4, None),
        # Looks like Fernet ciphertext but isn't valid for the configured key.
        (2, "gAAAAABnotARealTokenForThisKey"),
        (4, "gAAAAABnotARealTokenForThisKey"),
    ],
)
def test_aMissingOrUndecryptableValueStopsTheMigrationBeforeAnythingChanges(field: int, value: str | None) -> None:
    readable = _encrypted("PAT-000001", "Maria", "Santos", "1990-04-12")
    broken = list(_plaintext("PAT-000009", "Juan", "Dela Cruz", "1985-11-03"))
    broken[field] = value
    migration = _loadMigration()
    fakeOp = _FakeOp([readable, tuple(broken)])

    with patch.object(migration, "op", fakeOp.op), pytest.raises(RuntimeError) as excInfo:
        migration.upgrade()

    message = str(excInfo.value)
    assert "PAT-000009" in message
    assert "PAT-000001" not in message
    assert "1 patient record(s)" in message
    assert "Juan" not in message and "Dela Cruz" not in message
    assert fakeOp.updates == {}
    assert fakeOp.steps == ["ALTER TABLE patients ADD COLUMN IF NOT EXISTS dedup_hash VARCHAR(64)"]


def test_twoPatientsWithTheSameNameAndBirthDateStopTheMigration() -> None:
    # One stored as legacy plaintext, one encrypted: still the same person.
    legacy = _plaintext("PAT-000002", "Juan", "Dela Cruz", "1985-11-03")
    encrypted = _encrypted("PAT-000007", "juan", "DELA CRUZ", "1985-11-03")
    migration = _loadMigration()
    fakeOp = _FakeOp([legacy, encrypted, _encrypted("PAT-000001", "Maria", "Santos", "1990-04-12")])

    with patch.object(migration, "op", fakeOp.op), pytest.raises(RuntimeError) as excInfo:
        migration.upgrade()

    message = str(excInfo.value)
    assert "PAT-000002" in message and "PAT-000007" in message
    assert "PAT-000001" not in message
    assert "duplicate name+date-of-birth" in message
    assert fakeOp.updates == {}
    assert "SET NOT NULL" not in " ".join(fakeOp.steps)
