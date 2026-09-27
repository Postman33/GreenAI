"""Idempotently seed the plant catalog and normative rules in PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg


DEFAULT_DSN = "postgresql://admin:admin@localhost:5432/admin"


@dataclass(frozen=True)
class NormDocument:
    code: str
    title: str
    edition: str | None
    effective_from: date | None
    source_url: str
    note: str | None = None


@dataclass(frozen=True)
class PlacementRule:
    code: str
    norm_code: str
    plant_type: str
    target_object: str
    conditions: dict[str, Any]
    norm_reference: str


@dataclass(frozen=True)
class Plant:
    name: str
    plant_type: str
    min_spacing_m: float | None = None
    recommended_spacing_m: float | None = None
    mature_crown_radius_m: float | None = None
    dimension_source: str | None = None
    selection_priority: int = 100
    hardiness_zone_min: int | None = None
    hardiness_zone_max: int | None = None
    climate_suitability: str = "conditional"
    hardiness_source: str | None = None
    is_invasive: bool = False
    is_toxic: bool | None = None
    is_thorny: bool | None = None


PLANT_CATALOG_SOURCE = (
    "Пилотный проект 20 улиц/3. 3-я Парковая/Проектное решение/PDF/"
    "03_Пояснительная записка.pdf, таблицы 2-4"
)


NORM_DOCUMENTS = (
    NormDocument(
        code="SP42_2026",
        title="СП 42.13330.2026. Градостроительство",
        edition="2026",
        effective_from=date(2026, 7, 12),
        source_url=(
            "https://protect.gost.ru/sp/details/"
            "f6917ab4-63d8-4ecb-9794-0b4990ba3b99"
        ),
        note="Основные минимальные расстояния до деревьев и кустарников.",
    ),
    NormDocument(
        code="PP743_MOSCOW",
        title=(
            "Постановление Правительства Москвы от 10.09.2002 № 743-ПП"
        ),
        edition=None,
        effective_from=None,
        source_url=(
            "https://www.mos.ru/upload/documents/files/7389/"
            "Postanovlenie743-PP.pdf"
        ),
        note="Охрана существующих насаждений и ориентиры шага посадки.",
    ),
    NormDocument(
        code="SP82_2016",
        title="СП 82.13330.2016. Благоустройство территорий",
        edition="с изменениями № 1–3",
        effective_from=None,
        source_url=(
            "https://protect.gost.ru/sp/details/"
            "8d5a8ef5-f450-4356-ac88-72cd17c416cf"
        ),
        note="Ограничения для токсичных и колючих растений и доступности.",
    ),
    NormDocument(
        code="SP59_2020",
        title="СП 59.13330.2020. Доступность зданий и сооружений для МГН",
        edition="с изменениями № 1–4",
        effective_from=None,
        source_url=(
            "https://protect.gost.ru/sp/details/"
            "9a314146-2e12-458d-a3dc-914eaa364d51"
        ),
        note="Проверки доступных пешеходных маршрутов.",
    ),
    NormDocument(
        code="PP369_MOSCOW_2026",
        title=(
            "Постановление Правительства Москвы от 03.03.2026 № 369-ПП"
        ),
        edition="2026",
        effective_from=date(2026, 3, 3),
        source_url=(
            "https://vestnikmoscow.mos.ru/wp-content/uploads/2026/03/"
            "zhurnal-vestnik-moskvy-%E2%84%96-14.pdf"
        ),
        note="Перечень инвазивных растений и меры по их регулированию.",
    ),
)


# Names and classes are transcribed from the 3rd Parkovaya project schedule.
# The schedule does not specify per-species dimensions, so unverified values
# remain NULL. The two species used by the MVP have explicit project assumptions
# matching the checked plugin configuration; they are deliberately labelled as
# assumptions rather than normative requirements. The invasive flags follow
# Moscow Government Resolution No. 369-PP (2026).
MVP_DIMENSION_SOURCE = (
    "Проектное допущение MVP из greenai.plugin.json; "
    "перед рабочим проектированием уточнить по данным питомника и дендролога"
)

HARDINESS_SOURCE = (
    "Предварительный отбор GreenAI по диапазонам USDA/RHS Plant Finder; "
    "для рабочей документации подтвердить конкретный сорт и партию у питомника"
)


def hardy_plant(
    name: str,
    plant_type: str,
    zone_min: int,
    zone_max: int,
    **kwargs: Any,
) -> Plant:
    """Create a Moscow-screened catalog entry with traceable hardiness data."""
    suitability = str(kwargs.pop("climate_suitability", "recommended"))
    return Plant(
        name,
        plant_type,
        hardiness_zone_min=zone_min,
        hardiness_zone_max=zone_max,
        climate_suitability=suitability,
        hardiness_source=HARDINESS_SOURCE,
        **kwargs,
    )


PLANTS = (
    hardy_plant("Сосна обыкновенная", "tree", 2, 7),
    hardy_plant("Ель колючая", "tree", 2, 7),
    hardy_plant("Береза бумажная", "tree", 2, 7),
    hardy_plant("Клён остролистный 'Drummondii'", "tree", 4, 7),
    hardy_plant(
        "Липа мелколистная 'Winter Orange'",
        "tree",
        3,
        7,
        min_spacing_m=5.0,
        recommended_spacing_m=6.0,
        mature_crown_radius_m=2.5,
        dimension_source=MVP_DIMENSION_SOURCE,
        selection_priority=10,
    ),
    hardy_plant("Клён Гиннала", "shrub", 2, 8),
    hardy_plant("Можжевельник казацкий", "shrub", 3, 7),
    hardy_plant("Можжевельник средний", "shrub", 4, 9),
    hardy_plant("Сосна горная", "shrub", 2, 7),
    hardy_plant("Туя западная Глобоза", "shrub", 3, 7),
    hardy_plant("Туя западная Даника", "shrub", 3, 7),
    hardy_plant("Сирень обыкновенная", "shrub", 3, 7),
    hardy_plant("Сирень венгерская", "shrub", 3, 7),
    hardy_plant("Ирга Ламарка", "shrub", 4, 8),
    hardy_plant(
        "Спирея серая",
        "shrub",
        4,
        8,
        min_spacing_m=1.5,
        recommended_spacing_m=2.0,
        mature_crown_radius_m=0.75,
        dimension_source=MVP_DIMENSION_SOURCE,
        selection_priority=10,
    ),
    hardy_plant("Гортензия древовидная", "shrub", 3, 9),
    hardy_plant("Гортензия метельчатая", "shrub", 3, 8),
    hardy_plant("Дёрен белый", "shrub", 2, 7, is_invasive=True),
    hardy_plant("Дёрен белый 'Elegantissima'", "shrub", 3, 7, is_invasive=True),
    hardy_plant("Рябинник 'Sem'", "shrub", 3, 7, is_invasive=True),
    hardy_plant("Кизильник блестящий", "shrub", 3, 7),
    hardy_plant("Ива пурпурная 'Nana'", "shrub", 4, 8),
    hardy_plant("Спирея японская", "shrub", 3, 8),
    hardy_plant("Спирея березолистная", "shrub", 3, 8),
    hardy_plant("Пузыреплодник калинолистный", "shrub", 2, 8, is_invasive=True),
    hardy_plant("Спирея березолистная Тор", "shrub", 3, 8),
    hardy_plant("Боярышник Поль Скарлет", "shrub", 4, 8, is_thorny=True),
    # Functional grass-cover option required by the case in addition to the
    # species list extracted from the project planting schedules. Its exact
    # seed mix must be specified by the landscape designer before construction.
    hardy_plant("Газонная травосмесь для городских территорий", "herbaceous", 4, 8, min_spacing_m=0.0, selection_priority=10, climate_suitability="conditional"),
    hardy_plant("Астильба китайская", "herbaceous", 4, 8),
    hardy_plant("Бруннера крупнолистная", "herbaceous", 3, 8),
    hardy_plant("Бузульник Пржевальского", "herbaceous", 4, 8),
    hardy_plant("Вейник остроцветковый 'Карл Форстер'", "herbaceous", 4, 9),
    hardy_plant("Вербена бонарская", "herbaceous", 7, 11, climate_suitability="seasonal_only"),
    hardy_plant("Волжанка двудомная", "herbaceous", 3, 7),
    hardy_plant("Герань гибридная", "herbaceous", 4, 8, climate_suitability="conditional"),
    hardy_plant("Герань крупнокорневищная", "herbaceous", 4, 8),
    hardy_plant("Горец родственный", "herbaceous", 5, 8, climate_suitability="conditional"),
    hardy_plant("Дербенник иволистный", "herbaceous", 3, 9),
    hardy_plant("Котовник Фассена", "herbaceous", 3, 8),
    hardy_plant("Лилейник гибридный 'Stella De Oro'", "herbaceous", 3, 9),
    hardy_plant("Манжетка мягкая", "herbaceous", 3, 8),
    hardy_plant("Очиток видный (сорта)", "herbaceous", 3, 9, climate_suitability="conditional"),
    hardy_plant("Роджерсия конскокаштанолистная", "herbaceous", 5, 7, climate_suitability="conditional"),
    hardy_plant("Сныть обыкновенная 'Variegata'", "herbaceous", 4, 9),
    hardy_plant("Тиарелла сердцелистная", "herbaceous", 4, 9),
    hardy_plant("Фалярис тростниковый", "herbaceous", 3, 9),
    hardy_plant("Хоста гибридная", "herbaceous", 3, 9, climate_suitability="conditional"),
    hardy_plant("Щучка дернистая", "herbaceous", 4, 9),
)


def distance_rule(
    code: str,
    plant_type: str,
    target_object: str,
    distance_m: float,
) -> PlacementRule:
    return PlacementRule(
        code=code,
        norm_code="SP42_2026",
        plant_type=plant_type,
        target_object=target_object,
        conditions={
            "check": "min_distance",
            "min_distance_m": distance_m,
            "measure_to": "plant_axis",
        },
        norm_reference="СП 42.13330.2026, таблица 6.3",
    )


PLACEMENT_RULES = (
    distance_rule("TREE_BUILDING_5", "tree", "building", 5.0),
    distance_rule("TREE_SIDEWALK_0_7", "tree", "sidewalk", 0.7),
    distance_rule("TREE_ROAD_EDGE_2", "tree", "road_edge", 2.0),
    # Применяется только к очищенной моделью геометрии газопровода CLEAN_*.
    # Расстояние измеряется от оси ствола дерева до оси газопровода.
    distance_rule("TREE_GAS_1_5", "tree", "gas_pipe", 1.5),
    # Эти расстояния применяются только к геометрии CLEAN_* после отдельного
    # этапа очистки инженерных сетей. Сырые CAD-слои для buffer не используются.
    distance_rule("TREE_HEAT_2", "tree", "heat_pipe", 2.0),
    distance_rule("TREE_WATER_2", "tree", "water_pipe", 2.0),
    distance_rule("TREE_POWER_CABLE_2", "tree", "power_cable", 2.0),
    distance_rule("SHRUB_BUILDING_1_5", "shrub", "building", 1.5),
    distance_rule("SHRUB_SIDEWALK_0_5", "shrub", "sidewalk", 0.5),
    distance_rule("SHRUB_ROAD_EDGE_1", "shrub", "road_edge", 1.0),
    distance_rule("SHRUB_HEAT_1", "shrub", "heat_pipe", 1.0),
    distance_rule("SHRUB_POWER_CABLE_0_75", "shrub", "power_cable", 0.75),
)

# Legacy rows remain in existing databases until seed.py is run again. The
# loader also ignores manual_review rows, so an ordinary pipeline run uses only
# computable rules without requiring a database reseed.
RETIRED_MANUAL_RULE_CODES = (
    "TREE_SEWER_1_5",
    "TREE_DRAINAGE_2",
    "TREE_TELECOM_MANUAL",
    "TREE_OVERHEAD_POWER_MANUAL",
    "SHRUB_GAS_MANUAL",
    "SHRUB_SEWER_MANUAL",
    "SHRUB_WATER_MANUAL",
    "SHRUB_DRAINAGE_MANUAL",
    "SHRUB_TELECOM_MANUAL",
    "SHRUB_OVERHEAD_POWER_MANUAL",
)


UPSERT_NORM_DOCUMENT = """
INSERT INTO norm_documents (
    code, title, edition, effective_from, source_url, note
)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (code) DO UPDATE SET
    title = EXCLUDED.title,
    edition = EXCLUDED.edition,
    effective_from = EXCLUDED.effective_from,
    source_url = EXCLUDED.source_url,
    note = EXCLUDED.note
"""

UPSERT_PLACEMENT_RULE = """
INSERT INTO placement_rules (
    rule_code,
    norm_document_id,
    source_plant_from,
    target_object_to,
    conditions,
    norm_reference
)
SELECT %s, document.id, %s, %s, %s::jsonb, %s
FROM norm_documents AS document
WHERE document.code = %s
ON CONFLICT (rule_code) DO UPDATE SET
    norm_document_id = EXCLUDED.norm_document_id,
    source_plant_from = EXCLUDED.source_plant_from,
    target_object_to = EXCLUDED.target_object_to,
    conditions = EXCLUDED.conditions,
    norm_reference = EXCLUDED.norm_reference
"""


UPSERT_PLANT = """
INSERT INTO plant_catalog (
    name,
    plant_type,
    min_spacing_m,
    recommended_spacing_m,
    mature_crown_radius_m,
    dimension_source,
    selection_priority,
    hardiness_zone_min,
    hardiness_zone_max,
    climate_suitability,
    hardiness_source,
    is_invasive,
    is_toxic,
    is_thorny
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE SET
    plant_type = EXCLUDED.plant_type,
    min_spacing_m = EXCLUDED.min_spacing_m,
    recommended_spacing_m = EXCLUDED.recommended_spacing_m,
    mature_crown_radius_m = EXCLUDED.mature_crown_radius_m,
    dimension_source = EXCLUDED.dimension_source,
    selection_priority = EXCLUDED.selection_priority,
    hardiness_zone_min = EXCLUDED.hardiness_zone_min,
    hardiness_zone_max = EXCLUDED.hardiness_zone_max,
    climate_suitability = EXCLUDED.climate_suitability,
    hardiness_source = EXCLUDED.hardiness_source,
    is_invasive = EXCLUDED.is_invasive,
    is_toxic = EXCLUDED.is_toxic,
    is_thorny = EXCLUDED.is_thorny
"""


def ensure_compatible_schema(
    cursor: psycopg.Cursor[Any],
) -> tuple[set[str], set[str]]:
    """Upgrade the empty legacy prototype without deleting its data."""
    cursor.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'common_name'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'name'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog '
                        'RENAME COLUMN common_name TO name';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'selection_priority'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'selection_priority SMALLINT NOT NULL DEFAULT 100 '
                        'CHECK (selection_priority >= 0)';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'min_spacing_m'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog '
                        'ALTER COLUMN min_spacing_m DROP NOT NULL';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'recommended_spacing_m'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'recommended_spacing_m NUMERIC(5,2) '
                        'CHECK (recommended_spacing_m >= 0)';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'mature_crown_radius_m'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'mature_crown_radius_m NUMERIC(5,2) '
                        'CHECK (mature_crown_radius_m >= 0)';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'dimension_source'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN dimension_source TEXT';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'hardiness_zone_min'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'hardiness_zone_min SMALLINT '
                        'CHECK (hardiness_zone_min BETWEEN 1 AND 13)';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'hardiness_zone_max'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'hardiness_zone_max SMALLINT '
                        'CHECK (hardiness_zone_max BETWEEN 1 AND 13)';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'climate_suitability'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN '
                        'climate_suitability TEXT NOT NULL DEFAULT ''conditional'' '
                        'CHECK (climate_suitability IN '
                        '(''recommended'', ''conditional'', ''seasonal_only''))';
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'hardiness_source'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog ADD COLUMN hardiness_source TEXT';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'is_toxic'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog '
                        'ALTER COLUMN is_toxic DROP NOT NULL';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'plant_catalog'
                  AND column_name = 'is_thorny'
            ) THEN
                EXECUTE 'ALTER TABLE plant_catalog '
                        'ALTER COLUMN is_thorny DROP NOT NULL';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM pg_index AS index_info
                JOIN pg_class AS table_info
                  ON table_info.oid = index_info.indrelid
                JOIN pg_namespace AS schema_info
                  ON schema_info.oid = table_info.relnamespace
                JOIN pg_attribute AS attribute_info
                  ON attribute_info.attrelid = table_info.oid
                 AND attribute_info.attnum = ANY(index_info.indkey)
                WHERE schema_info.nspname = 'public'
                  AND table_info.relname = 'plant_catalog'
                  AND index_info.indisunique
                  AND index_info.indnatts = 1
                  AND attribute_info.attname = 'name'
            ) THEN
                EXECUTE 'CREATE UNIQUE INDEX plant_catalog_name_seed_uidx '
                        'ON plant_catalog (name)';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'placement_rules'
                  AND column_name = 'plant_type'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'placement_rules'
                  AND column_name = 'source_plant_from'
            ) THEN
                EXECUTE 'ALTER TABLE placement_rules '
                        'RENAME COLUMN plant_type TO source_plant_from';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'placement_rules'
                  AND column_name = 'source_object_type'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'placement_rules'
                  AND column_name = 'target_object_to'
            ) THEN
                EXECUTE 'ALTER TABLE placement_rules '
                        'RENAME COLUMN source_object_type TO target_object_to';
            END IF;

            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'placement_rules'
                  AND column_name = 'min_distance_m'
            ) THEN
                EXECUTE 'ALTER TABLE placement_rules '
                        'ALTER COLUMN min_distance_m DROP NOT NULL';
            END IF;
        END
        $$
        """
    )
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'placement_rules'
        """
    )
    placement_columns = {row[0] for row in cursor.fetchall()}
    required = {
        "rule_code",
        "norm_document_id",
        "source_plant_from",
        "target_object_to",
        "conditions",
        "norm_reference",
    }
    missing = required - placement_columns
    if missing:
        raise RuntimeError(
            "placement_rules has an unsupported schema; missing columns: "
            + ", ".join(sorted(missing))
        )
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'plant_catalog'
        """
    )
    plant_columns = {row[0] for row in cursor.fetchall()}
    required_plant_columns = {
        "name",
        "plant_type",
        "min_spacing_m",
        "recommended_spacing_m",
        "mature_crown_radius_m",
        "dimension_source",
        "selection_priority",
        "hardiness_zone_min",
        "hardiness_zone_max",
        "climate_suitability",
        "hardiness_source",
        "is_invasive",
        "is_toxic",
        "is_thorny",
    }
    missing_plant_columns = required_plant_columns - plant_columns
    if missing_plant_columns:
        raise RuntimeError(
            "plant_catalog has an unsupported schema; missing columns: "
            + ", ".join(sorted(missing_plant_columns))
        )
    return placement_columns, plant_columns


def seed(dsn: str) -> tuple[int, int, int]:
    """Insert or update all seed rows in one transaction."""
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            placement_columns, _plant_columns = ensure_compatible_schema(cursor)
            for document in NORM_DOCUMENTS:
                cursor.execute(
                    UPSERT_NORM_DOCUMENT,
                    (
                        document.code,
                        document.title,
                        document.edition,
                        document.effective_from,
                        document.source_url,
                        document.note,
                    ),
                )
            for rule in PLACEMENT_RULES:
                cursor.execute(
                    UPSERT_PLACEMENT_RULE,
                    (
                        rule.code,
                        rule.plant_type,
                        rule.target_object,
                        json.dumps(rule.conditions, ensure_ascii=False),
                        rule.norm_reference,
                        rule.norm_code,
                    ),
                )

            cursor.execute(
                """
                DELETE FROM placement_rules
                WHERE rule_code = ANY(%s)
                  AND conditions->>'check' = 'manual_review'
                """,
                (list(RETIRED_MANUAL_RULE_CODES),),
            )

            for plant in PLANTS:
                cursor.execute(
                    UPSERT_PLANT,
                    (
                        plant.name,
                        plant.plant_type,
                        plant.min_spacing_m,
                        plant.recommended_spacing_m,
                        plant.mature_crown_radius_m,
                        plant.dimension_source,
                        plant.selection_priority,
                        plant.hardiness_zone_min,
                        plant.hardiness_zone_max,
                        plant.climate_suitability,
                        plant.hardiness_source,
                        plant.is_invasive,
                        plant.is_toxic,
                        plant.is_thorny,
                    ),
                )

            # Preserve compatibility with the earlier prototype schema while
            # conditions remains the canonical representation for new code.
            if "min_distance_m" in placement_columns:
                cursor.execute(
                    """
                    UPDATE placement_rules
                    SET min_distance_m = CASE
                        WHEN conditions->>'check' = 'min_distance'
                        THEN (conditions->>'min_distance_m')::numeric
                        ELSE NULL
                    END
                    WHERE rule_code = ANY(%s)
                    """,
                    ([rule.code for rule in PLACEMENT_RULES],),
                )

            cursor.execute("SELECT COUNT(*) FROM norm_documents")
            document_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM placement_rules")
            rule_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM plant_catalog")
            plant_count = cursor.fetchone()[0]
    return document_count, rule_count, plant_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed plants, normative documents and placement rules."
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", DEFAULT_DSN),
        help=(
            "PostgreSQL DSN. Defaults to DATABASE_URL or the local "
            "admin/admin/admin Docker Compose database."
        ),
    )
    args = parser.parse_args()
    try:
        document_count, rule_count, plant_count = seed(args.dsn)
    except (psycopg.Error, RuntimeError) as error:
        raise SystemExit(f"Database seed failed: {error}") from error

    print(f"Norm documents in database: {document_count}")
    print(f"Placement rules in database: {rule_count}")
    print(f"Plants in database: {plant_count}")
    print(f"Plant catalog source: {PLANT_CATALOG_SOURCE}")
    print(
        "Unverified plant dimensions and safety attributes remain NULL; "
        "MVP assumptions are explicitly labelled in dimension_source"
    )


if __name__ == "__main__":
    main()
