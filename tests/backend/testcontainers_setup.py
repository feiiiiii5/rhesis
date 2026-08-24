"""Ephemeral Postgres/Redis containers for the backend test session.

Must run before any backend/app module is imported — tests.backend.fixtures.database
reads DB_HOST/DB_PORT to build its module-level engine at import time.

Each pytest-xdist worker starts its own container pair rather than sharing one:
a container handle only works in the process that created it, and a shared
container risks a worker tearing it down mid-test for its siblings.
"""

from __future__ import annotations

import atexit


def ensure_test_containers() -> dict:
    """Start this process's own Postgres/Redis containers.

    Returns a dict with ``db_host``/``db_port``/``redis_host``/``redis_port`` plus the
    database credentials tests should connect with: ``db_user``/``db_password`` are a
    non-superuser RLS subject (issue #2525), and ``admin_user``/``admin_password`` are
    the container superuser, reserved for migrations and role setup.
    """
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    postgres = PostgresContainer(
        image="mirror.gcr.io/pgvector/pgvector:pg16",
        username="rhesis-user",
        password="your-secured-password",  # trufflehog:ignore
        dbname="rhesis-test-db",
    )
    postgres.with_command("postgres -c max_connections=200")
    postgres.with_kwargs(tmpfs={"/var/lib/postgresql/data": "rw"})
    postgres.start()
    atexit.register(postgres.stop)

    app_db_user, app_db_password = _create_rls_subject_role(
        postgres,
        role_name="rhesis-app",
        role_password="rhesis-app-pass",  # trufflehog:ignore
    )

    redis = RedisContainer(
        image="mirror.gcr.io/redis:7-alpine",
        password="rhesis-redis-pass",
    )
    redis.start()
    atexit.register(redis.stop)

    return {
        "db_host": postgres.get_container_host_ip(),
        "db_port": postgres.get_exposed_port(5432),
        "db_user": app_db_user,
        "db_password": app_db_password,
        "admin_user": postgres.username,
        "admin_password": postgres.password,
        "redis_host": redis.get_container_host_ip(),
        "redis_port": redis.get_exposed_port(6379),
    }


def _create_rls_subject_role(postgres, role_name: str, role_password: str) -> tuple[str, str]:
    """Create the non-superuser role that tests connect as, mirroring production.

    ``POSTGRES_USER`` creates a *superuser*, and a superuser bypasses Row Level
    Security unconditionally — so a suite connecting as it can never exercise
    RLS policies (issue #2525). Production's runtime role has ``bypassrls:
    false`` and is not the table owner; replicate that split here: migrations
    keep running as the container superuser, while this NOBYPASSRLS role is the
    identity the test engine and application code use. Default privileges make
    every table/sequence the migrations create accessible to it.
    """
    import sqlalchemy

    engine = sqlalchemy.create_engine(
        postgres.get_connection_url(),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(
                text(
                    f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{role_password}' "
                    "NOSUPERUSER NOBYPASSRLS"
                )
            )
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{postgres.dbname}" TO "{role_name}"'))
            conn.execute(text(f'GRANT ALL ON SCHEMA public TO "{role_name}"'))
            conn.execute(
                text(
                    f'ALTER DEFAULT PRIVILEGES FOR ROLE "{postgres.username}" IN SCHEMA public '
                    f'GRANT ALL ON TABLES TO "{role_name}"'
                )
            )
            conn.execute(
                text(
                    f'ALTER DEFAULT PRIVILEGES FOR ROLE "{postgres.username}" IN SCHEMA public '
                    f'GRANT ALL ON SEQUENCES TO "{role_name}"'
                )
            )
    finally:
        engine.dispose()
    return role_name, role_password
