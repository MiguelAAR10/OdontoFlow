from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import audit  # noqa: F401  (register audit models)
from app import catalog  # noqa: F401  (register catalog models)
from app import commercial  # noqa: F401  (register commercial models)
from app import organization  # noqa: F401  (register organization models)
from app import scheduling  # noqa: F401  (register scheduling models)
from app.audit.models import AuditEvent  # noqa: F401
from app.catalog.models import Service  # noqa: F401
from app.commercial.models import Lead  # noqa: F401
from app.organization.models import Location, Practitioner, PractitionerCapability  # noqa: F401
from app.scheduling.models import Appointment, AvailabilityRule, ScheduleBlock  # noqa: F401
from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
