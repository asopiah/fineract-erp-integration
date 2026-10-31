from integration_service.domain.erp import ErpAccount
from integration_service.domain.gl_accounts import SyncStatus
from tests.component.conftest import System
from tests.component.fakes import Fault


def account(currency: str = "RWF") -> ErpAccount:
    return ErpAccount(code="1100", name="Bank", type="ASSET", currency=currency, external_reference="GL-1100")


def test_an_account_is_synced_once_however_often_it_is_sent(system: System) -> None:
    system.erp.accounts.append(account())

    for _ in range(4):
        [row] = system.gl_sync.sync_chart()

    assert row.status is SyncStatus.SYNCED
    assert system.fineract.commands("create_gl_account") == 1


def test_an_account_in_another_currency_is_not_synced(system: System) -> None:
    system.erp.accounts.append(account(currency="USD"))

    [row] = system.gl_sync.sync_chart()

    assert row.status is SyncStatus.FAILED
    assert row.next_attempt_at is None
    assert "USD" in (row.last_error or "")
    assert system.fineract.commands("create_gl_account") == 0


def test_a_sync_that_failed_while_fineract_was_down_is_retried_from_the_erp(system: System) -> None:
    system.erp.accounts.append(account())
    system.fineract.fail("create_gl_account", Fault.NOT_APPLIED)

    [failed] = system.gl_sync.sync_chart()
    retried = system.gl_sync.retry_due()

    assert failed.status is SyncStatus.FAILED
    assert retried == 1
    assert system.gl_sync.statuses()[0].status is SyncStatus.SYNCED
