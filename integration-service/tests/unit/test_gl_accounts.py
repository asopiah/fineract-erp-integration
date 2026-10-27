import pytest

from integration_service.domain.gl_accounts import (
    ERP_TO_FINERACT_GL_TYPE,
    FineractGlType,
    UnsupportedAccountTypeError,
    fineract_gl_type,
)


@pytest.mark.parametrize(
    ("erp_type", "fineract_type"),
    [
        ("ASSET", FineractGlType.ASSET),
        ("LIABILITY", FineractGlType.LIABILITY),
        ("EQUITY", FineractGlType.EQUITY),
        ("INCOME", FineractGlType.INCOME),
        ("EXPENSE", FineractGlType.EXPENSE),
    ],
)
def test_every_erp_account_type_maps_to_its_fineract_gl_type(erp_type: str, fineract_type: FineractGlType) -> None:
    assert fineract_gl_type(erp_type) is fineract_type


def test_fineract_codes_are_the_ones_fineract_expects() -> None:
    assert [int(t) for t in FineractGlType] == [1, 2, 3, 4, 5]


def test_mapping_covers_every_fineract_type_once() -> None:
    assert sorted(ERP_TO_FINERACT_GL_TYPE.values()) == list(FineractGlType)


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(UnsupportedAccountTypeError):
        fineract_gl_type("CONTRA_ASSET")
