#!/bin/sh
# One PostgreSQL server, one owner role per service. Runs once, when the data volume is empty.
#
#   fineract     owns fineract_tenants and fineract_default (Fineract creates its own schema via Liquibase)
#   mock_erp     owns mock_erp
#   integration  owns integration
set -eu

create_role() {
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
        -c "CREATE ROLE $1 LOGIN PASSWORD '$2';"
}

create_database() {
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
        -c "CREATE DATABASE $1 OWNER $2;"
}

create_role fineract "$FINERACT_DB_PASSWORD"
create_database fineract_tenants fineract
create_database fineract_default fineract

create_role mock_erp "$MOCK_ERP_DB_PASSWORD"
create_database mock_erp mock_erp

create_role integration "$INTEGRATION_DB_PASSWORD"
create_database integration integration
