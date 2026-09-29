# Форматы данных и артефакты

Все пути ниже относятся к **одному** `-OutputDirectory`. Не смешивайте файлы разных прогонов: отчёт может ссылаться на другой план или модель. Текстовые файлы записываются как UTF-8. `.jsonl` и `.geojsonl` содержат один JSON-объект в строке; расширение `.geojsonl` здесь означает последовательность GeoJSON `Feature`, а не один GeoJSON `FeatureCollection`.

## Координаты, идентификаторы и источник

- Геометрия остаётся в локальной системе чертежа. В `properties.coordinate_reference` указано `local_dxf_coordinates`. Для нормативного расстояния в метрах используется `dxf_units_per_meter`, подтверждённое на стадии 02.
- `object_type` — внутренний семантический класс, например `building`, `road_edge`, `water_pipe`, `base_allowed_area`, `plant_allow_zone`, `proposed_planting`. Он не обязан совпадать с именем CAD-слоя.
- `source_layer`, `source_layer_tail`, `dxf_type`, `handle`, `block_path` в извлечённых записях позволяют проследить объект до исходника. Хвост после `$0$` важен для связанных/встроенных блоков.
- `planting_id` — стабильная связь между планом, решением, метаданными DXF, Markdown и PDF в рамках одного результата. Не используйте порядковую строку JSONL как ID. Формат `T-0001`, `S-0001`, `H-0001` зависит от типа посадки.
- `norm_reference` у проверки может указывать на конкретный НПА и таблицу/пункт или на явно обозначенное проектное допущение. Наличие ссылки в отчёте не доказывает полноту нормативного покрытия сети, для которой нет активного правила.

## Цепочка основных файлов

| Файл | Формат и смысл | Производитель → потребитель |
|---|---|---|
| `dxf_units_report.json` | `$INSUNITS`, масштаб и источник подтверждения единиц | `detect_dxf_units.py` → геометрические стадии |
| `extracted_objects.jsonl` | По одной **сырой CAD-записи**: тип, слой, handle, геометрия примитива | Go-парсер → нормализатор и детектор |
| `surface_candidates_raw.jsonl` | Широкий набор HATCH/замкнутых полилиний для классификации покрытий | Go-парсер → `constraint_builder` |
| `normalized_objects.geojsonl` | GeoJSON `Feature` по семантическому типу; геометрия после ремонта и объединения | нормализатор → ограничения и планировщик |
| `cleaned_utilities.geojsonl` | Принятые оси сетей после ONNX/эвристики | детектор → реконструктор |
| `review_utility_graphics.geojsonl`, `rejected_utility_graphics.geojsonl` | Неоднозначные и отвергнутые примитивы, для оценки качества | детектор → человек/диагностика |
| `reconstructed_utilities.geojsonl` | Принятая геометрия плюс допустимые добавленные соединения | реконструкторы → зоны/посадки |
| `inferred_utility_connections.geojsonl`, `review_utility_connections.geojsonl` | Объяснимые гипотезы соединения; review нельзя считать принятой осью | реконструктор → человек |
| `constraint_map.geojsonl` | Базовая допустимая поверхность, дорога, тротуары, покрытие, иногда подтверждённый газон и камеры | `constraint_builder` → зоны и верификатор |
| `plant_allow_zones.geojsonl` | По одному или нескольким полигонам допуска для каждого класса растений | `plant_allow_zone` → планировщик/экспорт |
| `planting_plan.geojsonl` | Принятые точечные/площадные посадки с видом и `checks` | `service.py` → экспорт, отчёты, верификация |
| `planting_decisions.geojsonl` | Решения по принятым и отклонённым кандидатам, включая диагностические точки | `service.py` → debug DXF и PDF |
| `planting_layout_trace.jsonl` | Варианты схем деревьев и кустарников, проектные оценки и выбранная схема; для деревьев — координаты каждого сравнивавшегося варианта | `service.py` → аудит раскладки |
| `planting_explanations.md` | Раздел на каждый ID принятой посадки | `service.py` → человек и верификатор |

Пример сокращённой записи посадки (поля `checks` внутри реального файла гораздо подробнее):

```json
{
  "type": "Feature",
  "id": "T-0001",
  "properties": {
    "object_type": "proposed_planting",
    "planting_id": "T-0001",
    "plant_type": "tree",
    "species": "Липа мелколистная",
    "status": "accepted",
    "dxf_units_per_meter": 1.0,
    "footprint_radius_m": 3.0,
    "checks": [
      {
        "code": "TREE_POWER_CABLE_2",
        "status": "passed",
        "actual_distance_m": 7.82,
        "required_distance_m": 2.0,
        "norm_reference": "СП 42.13330.2026, таблица 6.3",
        "explanation": "..."
      }
    ]
  },
  "geometry": {"type": "Point", "coordinates": [0, 0]}
}
```

Здесь `coordinates: [0, 0]` и `7.82` приведены **только для иллюстрации структуры** и не являются координатами/замером `T-0001` в демо. В реальном плане координаты и расстояния берутся из соответствующего файла прогона. Площадная посадка имеет `object_type: proposed_planting_area`, геометрию Polygon/MultiPolygon, `species_selection`, `checks` и поля оценки количества растений для некоторых стилей.

Каждый элемент `checks` содержит `code`, `target`, `status`, `actual_distance_m` при измеримой проверке, `required_distance_m` при пороге, `norm_reference`, `explanation`. Основные статусы — `passed`, `failed`, `manual_review`. Отсутствующее измерение пишется `null`, а не бесконечностью: JSON не допускает NaN/Infinity. Решение по кандидату дополнительно содержит `candidate_id`, `accepted_into_plan`, `diagnostic_candidate`, `failed_checks`, `manual_review_checks` и `rejection_reasons`.

## Отчёты для диагностики

| Файл | Что смотреть сначала |
|---|---|
| `normalization_report.json` | Число исходных примитивов, найденных/восстановленных зданий и причины отклонения контуров |
| `utility_cleaning_report.json` | Принято, отправлено на review и отклонено по каждому типу сети; пороги модели |
| `network_reconstruction_report.json` | Принятые/неоднозначные соединения и параметры поиска |
| `overhead_power_reconstruction_report.json` | Гипотезы ВЛ и стрелки без надёжной связи/подписи |
| `constraint_report.json` | Источник базовой поверхности; площади до/после вычитаний; статус дороги и `requires_visual_confirmation` |
| `plant_allow_zones_report.json` | Для `plant_types.<тип>.rules[]`: код, источник геометрии, статус, порог, площадь выреза; `unchecked_utility_object_types` |
| `planting_plan_report.json` | Количество точек и площадей, ручных проверок, отклонённых кандидатов, ссылка на trace и объяснения |
| `verification_report.json` | Итог `status`, `failures`, проверка посадок и, в полном режиме, `dxf_checks` |
| `pipeline_stage_timings.json`, `.csv`, `.md` | Время и статус каждой стадии, включая кэш и пропуск |
| `pipeline_run_parameters.json` | Фактический вход, выбранный bundle, конфигурации, режим и имя итогового DXF |

`plant_allow_zones_report.json` различает: `applied` — буфер построен; `unavailable` — для правила нет целевой геометрии; `manual_review` — геометрия есть, но автоматическое применение недостоверно; `unsupported` — тип проверки не реализован. `verified_by_available_rules` значит, что выполнены доступные настроенные проверки. Перечень `unchecked_utility_object_types` отражает сети, видимые в исходных данных, для которых не задано действующее правило у данного типа растения.

Проверку `verification_report.json` нельзя сводить к одному `status`: при демонстрации нужны также `road_quality`, `planting_plan_checks`, `provenance_checks`, `dxf_checks` и `failures`. В `lean`/`fast` поле `dxf_checks` может быть `null`, поскольку исходный DXF не входит в маленький оверлей. Для сохранности исходника используйте результат `full`.

## CAD и человекочитаемые выходы

| Артефакт | Содержит |
|---|---|
| `result_with_planting_plan.dxf` (`full`) | Копия исходного DXF плюс собственные `SYLVITECT_*` слои; исходный файл не перезаписывается |
| `planting_overlay.dxf` (`lean`/`fast`) | Только новые посадки и восстановленная дорога в координатах исходника; подосновы нет |
| `planting_diagnostics.dxf` | Контекст, `DEBUG_ALLOW_*`, `DEBUG_EXCL_*`, `DEBUG_CLEAN_*`, принятые/отклонённые точки и причины |
| `planting_diagnostics_legend.md` | Фактическая легенда диагностических слоёв данного запуска |
| `sylvitect_planting_report.pdf` | Список отклонённых точек, сводка, правила и паспорт каждого принятого объекта |
| `planting_plan_atlas.pdf`, `planting_area_schedule.json` | Обзор, фрагменты и ведомость площадей |

Основные проектные слои задаются в `src/cad_io/dxf_exporter.py`: `SYLVITECT_ZONE_TREE`, `SYLVITECT_ZONE_SHRUB`, `SYLVITECT_PLANT_TREE`, `SYLVITECT_PLANT_SHRUB`, `SYLVITECT_HERBACEOUS`, `SYLVITECT_RECONSTRUCTED_ROAD`. Точечная посадка экспортируется как `CIRCLE` и несёт XDATA с приложением `SYLVITECT` (`id`, тип, вид, статус и данные проверок). Площадная посадка представлена границей и HATCH. Аналитические слои полного плана могут быть выключены по умолчанию для читаемости; это не означает отсутствие геометрии. Не надо интерпретировать любой круг на кустарниковом слое как отдельный куст: проверяйте тип CAD-сущности и `planting_id`.

Отдельные `DEBUG_*` слои нужны для аудита, их список формируется по данным запуска. Например, `DEBUG_CLEAN_HEAT_PIPE` показывает принятую ось теплосети, а `DEBUG_EXCL_SHRUB_HEAT_1` — вырез по соответствующему правилу. `DEBUG_REJECTED_TREE` показывает диагностические точки отказа. Точные цвета и расшифровки находятся в `planting_diagnostics_legend.md`; цвет сам по себе не заменяет чтение `checks`.

### Существующие кустарники и композиция

Кустарники на исходных чертежах пока не распознаются надёжно как отдельные растения. Если есть подтверждённая инвентаризация, передайте её как GeoJSON `FeatureCollection` параметром `-ExistingShrubSurvey <файл>` в `run_pipeline.ps1` (или `--existing-shrub-survey` в `src.planting.service`). Координаты должны совпадать с локальными координатами DXF. Объект `existing_shrub` — точка с уникальным `id` и, по возможности, `properties.species`; `existing_shrub_mass` — полигон существующего массива. Пример формата: `examples/existing_shrub_survey.example.geojson` (координаты в нём условные).

`planting_plan_report.json` записывает предложения по существующим растениям в `composition_advisories`, а `planting_explanations.md` поясняет их. Для дерева сравниваются локальные варианты «сохранить существующее дерево» и «заменить его цельной группой новых деревьев». Предложение возникает только при блокировании как минимум двух мест, одном виновном дереве и существенном преимуществе альтернативы: `design_quality_gain >= minimum_quality_gain`. Отчёт хранит обе проектные оценки и `alternative_group_coordinates`. Баллы оценивают рисунок посадки, а не состояние дерева или законность удаления. Утверждённый план сохраняет дерево. Для кустарника предложение возникает только при конфликте одиночного размеченного куста известного вида с новым однородным массивом. Кусты в существующем массиве или рядом с другими кустами не помечаются. При неизвестном виде система предлагает сначала определить его. Предложения выводятся красными маркерами на `SYLVITECT_REMOVE_TREE_REVIEW` и `SYLVITECT_REMOVE_SHRUB_REVIEW`, с идентификатором и причиной в XDATA. Это проектные решения для проверки на месте: исходные объекты DXF не удаляются автоматически. Без инвентаризации кустов отчёт показывает `composition_review_status: not_evaluated_no_confirmed_shrub_survey`; проверка деревьев имеет отдельное поле `tree_composition_review_status`.

## Кэш и совместимость

`preprocessing_cache.json` содержит `schema_version`, `cache_key`, дату и подписи артефактов. `preprocessing_cache_status.json` фиксирует hit/miss и причину. При смене модели, исходника, масштаба, файла правок дороги или кода подготовки нужно заново вычислять зоны. Публичные контракты при рефакторинге — имена файлов, `object_type`, `planting_id`, структура `checks` и названия `SYLVITECT_*` слоёв; изменение этих полей затронет экспортер, PDF, диагностику и верификатор.
