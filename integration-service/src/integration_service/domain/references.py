"""Business identifiers issued by the integration service."""

import re

_LOAN_REFERENCE = re.compile(r"^LN-(\d{4})-(\d{6,})$")


def loan_reference(year: int, sequence: int) -> str:
    """``LN-2026-000001``: the year the application was submitted, then a gap-tolerant database sequence."""
    if sequence < 1:
        raise ValueError("loan reference sequence starts at 1")
    return f"LN-{year:04d}-{sequence:06d}"


def is_loan_reference(value: str) -> bool:
    return _LOAN_REFERENCE.fullmatch(value) is not None
