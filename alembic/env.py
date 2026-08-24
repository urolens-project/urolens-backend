from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import src.models.analysis_result  # noqa: F401
import src.models.audit_log  # noqa: F401
import src.models.image  # noqa: F401 — register tables with Base
import src.models.manual_override  # noqa: F401
import src.models.patient  # noqa: F401
import src.models.result_confirmation  # noqa: F401
import src.models.result_view  # noqa: F401
import src.models.smart_diagnosis_output  # noqa: F401
from alembic import context
from src.core.config import settings
from src.models.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

targetMetadata = Base.metadata


def runMigrationsOffline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=targetMetadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def runMigrationsOnline() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.databaseUrl
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=targetMetadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    runMigrationsOffline()
else:
    runMigrationsOnline()
