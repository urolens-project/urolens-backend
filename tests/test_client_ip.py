"""Unit tests — client IP addresses in `inet` columns.

`audit_logs.ip_address` and `result_retrievals.ip_address` are `inet` in the
database. Mapped as text, the IP was sent as `VARCHAR` and Postgres refused the
insert ("column ip_address is of type inet but expression is of type character
varying"), which failed every request that wrote an audit row in its own
transaction — found when `GET /sync/pull` returned 500.

- `clientIpOrNone`: real addresses pass through normalized; placeholders,
  empty values and non-addresses become `None`;
- the models clean every value written to their IP column, however it is set;
- the audit insert no longer sends the IP as `VARCHAR`;
- every model that maps an `ip_address` column maps it as `inet`;
- the best-effort (Supabase) audit path sends a placeholder as null.
"""
from __future__ import annotations

import ipaddress
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import INET, asyncpg

from src.core import audit_logger
from src.core.audit_logger import AuditLogger
from src.models import Base
from src.models.audit_log import AuditLog
from src.models.client_ip import clientIpOrNone
from src.models.result_retrieval import ResultRetrieval

ENTITY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000e1")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.168.77.25", "192.168.77.25"),
        ("  10.0.0.8 ", "10.0.0.8"),
        ("::1", "::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("fe80::1%eth0", "fe80::1"),
        (ipaddress.ip_address("127.0.0.1"), "127.0.0.1"),
    ],
)
def test_aRealAddressIsKeptInTheFormPostgresAccepts(value: object, expected: str) -> None:
    assert clientIpOrNone(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "unknown", "testclient", "localhost", "10.0.0", "10.0.0.8/24", "1.2.3.4:5678", 0])
def test_anythingThatIsNotAnAddressIsStoredAsNothing(value: object) -> None:
    assert clientIpOrNone(value) is None


@pytest.mark.parametrize("model", [AuditLog, ResultRetrieval])
@pytest.mark.parametrize(
    ("given", "stored"),
    [("192.168.77.25", "192.168.77.25"), (" 10.0.0.8 ", "10.0.0.8"), ("unknown", None), ("testclient", None), ("", None), (None, None)],
)
def test_theModelsCleanTheIpHoweverItIsSet(model: type, given: str | None, stored: str | None) -> None:
    built = model(ipAddress=given)
    assigned = model()
    assigned.ipAddress = given

    assert built.ipAddress == stored
    assert assigned.ipAddress == stored


def test_theAuditInsertDoesNotSendTheIpAsVarchar() -> None:
    stmt = insert(AuditLog).values(
        logId=uuid.uuid4(), eventType="SYNC_PULLED", entityType="user", entityId=ENTITY_ID, ipAddress="192.168.77.25"
    )

    sql = str(stmt.compile(dialect=asyncpg.dialect()))

    # The IP is the last value: bound bare, so Postgres reads it as the column's own type.
    assert sql.endswith("$5)"), sql
    assert "$5::VARCHAR" not in sql
    assert "$2::VARCHAR" in sql  # ordinary text columns are still sent as text


def test_theRetrievalInsertDoesNotSendTheIpAsVarcharEither() -> None:
    stmt = insert(ResultRetrieval).values(retrievalId=uuid.uuid4(), ipAddress="10.0.0.8")

    sql = str(stmt.compile(dialect=asyncpg.dialect()))

    assert sql.endswith("$2)"), sql
    assert "VARCHAR" not in sql


def test_everyModelWithAnIpAddressColumnMapsItAsInet() -> None:
    mapped = {
        table.name: isinstance(table.c.ip_address.type, INET)
        for table in Base.metadata.tables.values()
        if "ip_address" in table.c
    }

    assert mapped == {"audit_logs": True, "result_retrievals": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "stored"),
    [("192.168.77.25", "192.168.77.25"), ("unknown", None), ("testclient", None), (None, None)],
)
async def test_theSessionAuditRowBindsOnlyARealAddress(given: str | None, stored: str | None) -> None:
    db = MagicMock()

    await AuditLogger().record("SYNC_PULLED", entityType="user", entityId=ENTITY_ID, userId=None, db=db, ipAddress=given)

    [row] = [call.args[0] for call in db.add.call_args_list]
    assert row.ipAddress == stored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "sent"),
    [("10.0.0.8", "10.0.0.8"), ("unknown", None), (None, None)],
)
async def test_theBestEffortAuditPathSendsAPlaceholderAsNull(given: str | None, sent: str | None) -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute = AsyncMock()

    with patch.object(audit_logger, "supabase", sb):
        await AuditLogger().record("LOGIN_FAILED", entityType="auth", entityId=ENTITY_ID, userId=None, ipAddress=given)

    assert sb.table.return_value.insert.call_args.args[0]["ip_address"] == sent
