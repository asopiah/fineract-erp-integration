"""``python -m integration_service.migrate``: brings the database schema up to date."""

from integration_service.database import migrate
from integration_service.settings import Settings

if __name__ == "__main__":
    migrate(Settings().database_url)
