"""Client IP addresses as stored in Postgres `inet` columns.

`audit_logs.ip_address` and `result_retrievals.ip_address` are `inet` in the
database. Mapped as text, SQLAlchemy sent the value as `VARCHAR`, which Postgres
refuses to put in an `inet` column — so every audit row written in a request's
transaction failed, and the request with it. The models now map those columns as
`INET` and pass every value through `clientIpOrNone`, so a real address is bound
as an address and anything else is stored as NULL instead of failing the write.
"""
from __future__ import annotations

import ipaddress


def clientIpOrNone(value: object) -> str | None:
    """The value as an IP address Postgres `inet` accepts, or `None`.

    Callers pass whatever they have for the client: a real address, or a
    placeholder such as `"unknown"` when the request has no client. A
    placeholder isn't an address, so it is stored as NULL rather than failing
    the write.

    Returns:
        The normalized IPv4/IPv6 address (an IPv6 zone such as `%eth0` is
        dropped), or `None` for a missing, empty or non-address value.
    """
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id:
        address = ipaddress.IPv6Address(int(address))
    return str(address)
