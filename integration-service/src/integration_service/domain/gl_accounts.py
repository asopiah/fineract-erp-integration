"""GL account synchronization rules: the one place ERP account types map to Fineract GL account types."""

from enum import IntEnum, StrEnum


class SyncStatus(StrEnum):
    SYNC_PENDING = "SYNC_PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class FineractGlType(IntEnum):
    """Fineract's ``GLAccountType`` codes."""

    ASSET = 1
    LIABILITY = 2
    EQUITY = 3
    INCOME = 4
    EXPENSE = 5


class UnsupportedAccountTypeError(ValueError):
    pass


ERP_TO_FINERACT_GL_TYPE: dict[str, FineractGlType] = {
    "ASSET": FineractGlType.ASSET,
    "LIABILITY": FineractGlType.LIABILITY,
    "EQUITY": FineractGlType.EQUITY,
    "INCOME": FineractGlType.INCOME,
    "EXPENSE": FineractGlType.EXPENSE,
}


def fineract_gl_type(erp_type: str) -> FineractGlType:
    try:
        return ERP_TO_FINERACT_GL_TYPE[erp_type]
    except KeyError:
        raise UnsupportedAccountTypeError(f"ERP account type {erp_type!r} has no Fineract GL type") from None
