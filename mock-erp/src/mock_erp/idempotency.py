"""The ERP enforces its own uniqueness on ``external_reference``; it does not rely on callers behaving."""

from mock_erp.errors import ApiError, ErrorCode


def ensure_same_request(stored_fingerprint: str, request_fingerprint: str, external_reference: str) -> None:
    """A reused reference must carry the identical request; otherwise it names a different business operation."""
    if stored_fingerprint != request_fingerprint:
        raise ApiError(
            409,
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"external_reference {external_reference} was already used with a different payload",
        )
