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
    selection_priority: int = 100
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
# The source does not specify per-species spacing or toxicity; those fields
# remain NULL until a separate horticultural source is recorded. The invasive
# flags follow Moscow Government Resolution No. 369-PP (2026).
PLANTS = (
    Plant("Сосна обыкновенная", "tree"),
    Plant("Ель колючая", "tree"),
    Plant("Береза бумажная", "tree"),
    Plant("Клён остролистный 'Drummondii'", "tree"),
    Plant("Липа мелколистная 'Winter Orange'", "tree"),
    Plant("Клён Гиннала", "shrub"),
    Plant("Можжевельник казацкий", "shrub"),
    Plant("Можжевельник средний", "shrub"),
    Plant("Сосна горная", "shrub"),
    Plant("Туя западная Глобоза", "shrub"),
    Plant("Туя западная Даника", "shrub"),
    Plant("Сирень обыкновенная", "shrub"),
    Plant("Сирень венгерская", "shrub"),
    Plant("Ирга Ламарка", "shrub"),
    Plant("Спирея серая", "shrub"),
    Plant("Гортензия древовидная", "shrub"),
    Plant("Гортензия метельчатая", "shrub"),
    Plant("Дёрен белый", "shrub", is_invasive=True),
    Plant("Дёрен белый 'Elegantissima'", "shrub", is_invasive=True),
    Plant("Рябинник 'Sem'", "shrub", is_invasive=True),
    Plant("Кизильник блестящий", "shrub"),
    Plant("Ива пурпурная 'Nana'", "shrub"),
    Plant("Спирея японская", "shrub"),
    Plant("Спирея березолистная", "shrub"),
    Plant("Пузыреплодник калинолистный", "shrub", is_invasive=True),
    Plant("Спирея березолистная Тор", "shrub"),
    Plant("Боярышник Поль Скарлет", "shrub", is_thorny=True),
    Plant("Астильба китайская", "herbaceous"),
    Plant("Бруннера крупнолистная", "herbaceous"),
    Plant("Бузульник Пржевальского", "herbaceous"),
    Plant("Вейник остроцветковый 'Карл Форстер'", "herbaceous"),
    Plant("Вербена бонарская", "herbaceous"),
    Plant("Волжанка двудомная", "herbaceous"),
    Plant("Герань гибридная", "herbaceous"),
    Plant("Герань крупнокорневищная", "herbaceous"),
    Plant("Горец родственный", "herbaceous"),
    Plant("Дербенник иволистный", "herbaceous"),
    Plant("Котовник Фассена", "herbaceous"),
    Plant("Лилейник гибридный 'Stella De Oro'", "herbaceous"),
    Plant("Манжетка мягкая", "herbaceous"),
    Plant("Очиток видный (сорта)", "herbaceous"),
    Plant("Роджерсия конскокаштанолистная", "herbaceous"),
    Plant("Сныть обыкновенная 'Variegata'", "herbaceous"),
    Plant("Тиарелла сердцелистная", "herbaceous"),
    Plant("Фалярис тростниковый", "herbaceous"),
    Plant("Хоста гибридная", "herbaceous"),
    Plant("Щучка дернистая", "herbaceous"),
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


def manual_rule(
    code: str,
    plant_type: str,
    target_object: str,
    reason: str,
) -> PlacementRule:
    return PlacementRule(
        code=code,
        norm_code="SP42_2026",
        plant_type=plant_type,
        target_object=target_object,
        conditions={
            "check": "manual_review",
            "reason": reason,
        },
        norm_reference="СП 42.13330.2026, таблица 6.3 и примечания",
    )


PLACEMENT_RULES = (
    distance_rule("TREE_BUILDING_5", "tree", "building", 5.0),
    distance_rule("TREE_SIDEWALK_0_7", "tree", "sidewalk", 0.7),
    distance_rule("TREE_ROAD_EDGE_2", "tree", "road_edge", 2.0),
    manual_rule(
        "TREE_GAS_1_5",
        "tree",
        "gas_pipe",
        (
            "Нормативный отступ 1,5 м не применяется автоматически: "
            "исходный слой газопровода содержит трассы, окружности, "
            "замкнутые контуры и регулярные короткие штрихи."
        ),
    ),
    # Слой самотечной канализации смешивает трассы с окружностями, стрелками
    # и компактными условными знаками. До очистки геометрии отступ 1,5 м
    # проверяется вручную, а исходный слой сохраняется в debug DXF.
    manual_rule(
        "TREE_SEWER_1_5",
        "tree",
        "sewer_pipe",
        (
            "Нормативный отступ 1,5 м не применяется автоматически: "
            "исходный слой канализации содержит трассы и графические "
            "обозначения. Требуется ручная проверка или очистка геометрии."
        ),
    ),
    # Эти расстояния применяются только к геометрии CLEAN_* после отдельного
    # этапа очистки инженерных сетей. Сырые CAD-слои для buffer не используются.
    distance_rule("TREE_HEAT_2", "tree", "heat_pipe", 2.0),
    distance_rule("TREE_WATER_2", "tree", "water_pipe", 2.0),
    manual_rule(
        "TREE_DRAINAGE_2",
        "tree",
        "storm_drain",
        (
            "Нормативный отступ 2 м не применяется автоматически: слой "
            "водостока/дренажа содержит трассы, окружности, замкнутые "
            "контуры и большое количество коротких графических элементов."
        ),
    ),
    manual_rule(
        "TREE_POWER_CABLE_2",
        "tree",
        "power_cable",
        (
            "Нормативный отступ 2 м не применяется автоматически: слой "
            "силового кабеля содержит повторяющиеся короткие отрезки и "
            "ломаные графические обозначения вместе с трассами."
        ),
    ),
    manual_rule(
        "TREE_TELECOM_MANUAL",
        "tree",
        "telecom_cable",
        "Для кабеля связи таблица отсылает к отдельным документам.",
    ),
    manual_rule(
        "TREE_OVERHEAD_POWER_MANUAL",
        "tree",
        "overhead_power_line",
        "Охранная зона зависит от напряжения ЛЭП, которого нет во входном DXF.",
    ),
    distance_rule("SHRUB_BUILDING_1_5", "shrub", "building", 1.5),
    distance_rule("SHRUB_SIDEWALK_0_5", "shrub", "sidewalk", 0.5),
    distance_rule("SHRUB_ROAD_EDGE_1", "shrub", "road_edge", 1.0),
    distance_rule("SHRUB_HEAT_1", "shrub", "heat_pipe", 1.0),
    manual_rule(
        "SHRUB_POWER_CABLE_0_75",
        "shrub",
        "power_cable",
        (
            "Нормативный отступ 0,75 м не применяется автоматически: слой "
            "силового кабеля содержит повторяющиеся короткие отрезки и "
            "ломаные графические обозначения вместе с трассами."
        ),
    ),
    manual_rule(
        "SHRUB_GAS_MANUAL",
        "shrub",
        "gas_pipe",
        "Таблица 6.3 не задаёт расстояние для кустарника.",
    ),
    manual_rule(
        "SHRUB_SEWER_MANUAL",
        "shrub",
        "sewer_pipe",
        "Таблица 6.3 не задаёт расстояние для кустарника.",
    ),
    manual_rule(
        "SHRUB_WATER_MANUAL",
        "shrub",
        "water_pipe",
        "Таблица 6.3 не задаёт расстояние для кустарника.",
    ),
    manual_rule(
        "SHRUB_DRAINAGE_MANUAL",
        "shrub",
        "storm_drain",
        "Таблица 6.3 не задаёт расстояние для кустарника.",
    ),
    manual_rule(
        "SHRUB_TELECOM_MANUAL",
        "shrub",
        "telecom_cable",
        "Для кабеля связи таблица отсылает к отдельным документам.",
    ),
    manual_rule(
        "SHRUB_OVERHEAD_POWER_MANUAL",
        "shrub",
        "overhead_power_line",
        "Охранная зона зависит от напряжения ЛЭП, которого нет во входном DXF.",
    ),
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
    selection_priority,
    is_invasive,
    is_toxic,
    is_thorny
)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE SET
    plant_type = EXCLUDED.plant_type,
    min_spacing_m = EXCLUDED.min_spacing_m,
    selection_priority = EXCLUDED.selection_priority,
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
        "selection_priority",
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

            for plant in PLANTS:
                cursor.execute(
                    UPSERT_PLANT,
                    (
                        plant.name,
                        plant.plant_type,
                        plant.min_spacing_m,
                        plant.selection_priority,
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
    print("Plant spacing and toxicity remain NULL until separately verified")


if __name__ == "__main__":
    main()
