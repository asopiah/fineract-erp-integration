"""Request fingerprints: equal for semantically equal requests, different for anything else."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def fingerprint(request: BaseModel, **context: Any) -> str:
    """SHA-256 of the request's canonical JSON, plus any context that is part of the request but not of its body
    (such as the loan in the URL). ``5000000`` and ``5000000.00`` fingerprint the same."""
    payload = {"body": request.model_dump(), "context": context}
    canonical = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value
