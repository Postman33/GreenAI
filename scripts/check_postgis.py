"""Check whether the configured PostGIS database is ready for the pipeline."""

from __future__ import annotations

import os

import psycopg


DEFAULT_DSN = "postgresql://admin:admin@localhost:5432/admin"


def is_postgis_ready(dsn: str | None = None) -> bool:
    try:
        with psycopg.connect(
            dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN), connect_timeout=3
        ) as connection:
            present = connection.execute(
                """SELECT to_regclass('public.norm_documents') IS NOT NULL,
                          to_regclass('public.plant_catalog') IS NOT NULL,
                          to_regclass('public.placement_rules') IS NOT NULL"""
            ).fetchone()
            if present is None or not all(present):
                return False
            return connection.execute("SELECT postgis_version()").fetchone() is not None
    except (psycopg.Error, OSError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if is_postgis_ready() else 1)
