"""Alembic runs inside the service's already-open transaction."""

from alembic import context

connection = context.config.attributes["connection"]
context.configure(
    connection=connection, version_table="cardea_alembic_version", transactional_ddl=True
)
with context.begin_transaction():
    context.run_migrations()
