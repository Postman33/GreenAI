CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE norm_documents (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    edition TEXT,
    effective_from DATE,
    effective_to DATE,
    source_url TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE plant_catalog (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    plant_type TEXT NOT NULL CHECK (plant_type IN ('tree', 'shrub', 'herbaceous', 'groundcover')),
    min_spacing_m NUMERIC(5,2) NOT NULL CHECK (min_spacing_m >= 0),
    selection_priority SMALLINT NOT NULL DEFAULT 100 CHECK (selection_priority >= 0),
    is_invasive BOOLEAN NOT NULL DEFAULT FALSE,
    is_toxic BOOLEAN NOT NULL DEFAULT FALSE,
    is_thorny BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE plant_catalog IS
    'Каталог видов, из которых алгоритм выбирает растение для допустимой точки.';

COMMENT ON COLUMN plant_catalog.id IS
    'Внутренний идентификатор записи.';

COMMENT ON COLUMN plant_catalog.name IS
    'Название растения для выдачи в DXF и отчёте.';

COMMENT ON COLUMN plant_catalog.plant_type IS
    'Класс посадки: tree, shrub, herbaceous или groundcover.';

COMMENT ON COLUMN plant_catalog.min_spacing_m IS
    'Минимальное расстояние в метрах до другой новой посадки.';

COMMENT ON COLUMN plant_catalog.selection_priority IS
    'Приоритет выбора: меньшее число означает более предпочтительный вид.';

COMMENT ON COLUMN plant_catalog.is_invasive IS
    'Признак инвазивного вида; такие растения не рекомендуются.';

COMMENT ON COLUMN plant_catalog.is_toxic IS
    'Признак ядовитого растения; применяется при проверке ограничений точки.';

COMMENT ON COLUMN plant_catalog.is_thorny IS
    'Признак колючего растения; применяется при проверке расстояния до прохода.';

CREATE TABLE placement_rules (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_code TEXT NOT NULL UNIQUE,
    norm_document_id BIGINT REFERENCES norm_documents(id),
    source_plant_from TEXT CHECK,
    target_object_to TEXT NOT NULL,
    conditions JSONB NOT NULL DEFAULT '{}'::JSONB,
    norm_reference TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
/*
 conditions:
 [min_distance] - расстояние между объектов, не менее чем X метров
 [

 */


-- С индексами разберемся позже. todo
-- CREATE INDEX placement_rules_source_object_type_idx
--     ON placement_rules (source_object_type);
--
-- CREATE INDEX placement_rules_plant_type_idx
--     ON placement_rules (plant_type);


/* Мини-Архитектура

  1. Входные данные
  Парсим DFX => получем Document
  Используем ezdxf

  Результат: прочитали файл

  2. Распознавание объектов
    У нас есть конфиг слоев, по нему понимаем, как найти например РАСТЕНИЯ, ЗДАНИЯ, ТРОТУАРЫ, ИНЖЕНЕРНЫЕ ОБЪЕКТЫ
  Конфиг парсинга будем хранить в .json или в БД
  Результат: получили массивы объектов, допустимую границу в которой работаем.

 3. Формирование БД справочников
  Мы должны заполнить данные БД:
    а) Типы растений, пригодность для сажания (plant_catalog таблица)
    б) Список правил для КАЖДОГО ТИПА РАСТЕНИЯ (placement_rules таблица), подходят ли он под точку,
        например, деревья сажать не менее чем на 2м от водопровода, и чтобы до здания было 5 метров (это 2 правила)
        в каждом правиле есть ссылка на нормативку

   берем из файлов, заполняют базу PostGIS. Далее решим как. Таблицы сверху (прототип)
  Результат: запонленые справочники

 4. Проверка входа
      - DXF открылся;
      - есть граница работ;
      - найдены нужные слои;
      - единицы измерения — метры;
      - если слоя коммуникаций нет, фиксируем, какую проверку выполнить нельзя.

  5. Построение запретных зон

     Для каждого типа посадки берём его правила из placement_rules и строим буферы:
     здания + 5 м для деревьев
     дороги + 2 м для деревьев
     водопровод + 2 м для деревьев

     Объединяем буферы через ЛОГИЧЕСКОЕ И.

  6. Получение допустимых зон

     граница работ − запретные зоны = допустимая зона

     Отдельно для деревьев, кустарников и травянистых покрытий.

  7. Генерация кандидатов на посадку

     В допустимых зонах создаём точки-кандидаты. Сначала деревья, потом кустарники. После принятой посадки учитываем её min_spacing_m, чтобы новые точки не оказались слишком близко.

  8. Выбор растения для точки
      - берём растения нужного plant_type;
      - исключаем инвазивные;
      - применяем ограничения ядовитых и колючих растений;
      - выбираем растение с лучшим selection_priority.

  9. Формирование результата
      - записываем точки в новый DXF-слой;
      - исходные слои не меняем;
      - создаём JSON-отчёт: координаты, растение, применённые правила, причины отказа кандидатов, СТОИМОСТЬ
      - отдача DXF файла бекендом

  10. Добавить UX/UI
      - читает результат и объясняет что куда можно посадить с обоснованием (например, потому что все проверки для КЛАССА ДЕРЕВО пррошли успешно.)

  12. Добавить чат бота
      - Должен управлять параметрами ЛАНДШАФТА посадки, например форма посадки (архитектурный ландшафт)
      - Рекомендовать варианты посадки ТИПА РАСТЕНИЯ
      - Менять ГИПЕРПАРАМЕТРЫ МОДЕЛИ для уменьшения стоимости работ

  Вопросы:
  1. определиться с API
  2. Сервис расчета СТОИМОСТИ РАБОТ - как считать?
  3. Визуализация DXF в UI. Нужна ли карта?
  4. Чо делать с [Бонусное задание) после создания финального DXF-файла — сгенерировать
реалистичные фото/визуализации проекта озеленения] - я не понял. Возможно нужно LLM-ку захостить? Может спросим про то, дают ли API ключи? Вообще тут хз че делать
  5. Я так понимаю по каждой зоне, например пешеходной - предлагать суперпозиции, т.е. пользователь на выбор может
  ИЗМЕНИТЬ тип посадки, а не жестко скзаать что тут можно посадить ДЕРЕВО, а дать выбор.
  6. Подумать над таблицами справочинков
  7. Подумать над алгоритмами
  8. Подумать над UX/UI
  9. Типовые посадки - ГЕОМЕТРИЧЕСКИЙ СЕРВИС, нужно обдумать


  ФУНКЦИОНАЛЬНЫЕ ТРБЕОВАНИЯ:
    1. Перенос точек вручную на UI.
    2. Повторный запуск PIPELINE с изменением параметров, точек с UI
    3. Нельзя портить исходный DXF файл
 */