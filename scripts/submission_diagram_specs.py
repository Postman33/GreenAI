"""Single source for GitHub Mermaid and PDF diagram assets.

Coordinates are for the PDF rendering only. Nodes, groups and edges also produce
the Mermaid blocks embedded in the Markdown documentation.
"""

from __future__ import annotations


def group(key, label, x, y, w, h):
    return dict(key=key, label=label, x=x, y=y, w=w, h=h)


def node(key, label, parent, x, y, w=135, h=68, kind="process"):
    return dict(key=key, label=label, parent=parent, x=x, y=y, w=w, h=h, kind=kind)


def edge(source, target, label="", via=None):
    return dict(source=source, target=target, label=label, via=via or [])


DIAGRAMS = {
    "system_design": {
        "direction": "LR",
        "groups": [
            group("input", "Вход", 20, 35, 165, 445),
            group("prepare", "Подготовка данных", 200, 35, 330, 445),
            group("calculate", "Расчёт", 545, 35, 380, 445),
            group("deliver", "Результат", 940, 35, 235, 445),
        ],
        "nodes": [
            node("dxf", "Исходный DXF", "input", 35, 125, kind="source"),
            node("config", "Карта слоёв и\nпараметры", "input", 35, 320, kind="source"),
            node("extract", "Извлечение\nDXF", "prepare", 220, 125),
            node("normalize", "Геометрия\nповерхностей", "prepare", 380, 125),
            node("model", "ONNX: очистка\nсетей", "prepare", 220, 320, kind="model"),
            node("reconstruct", "Разрывы\nсетей", "prepare", 380, 320),
            node("constraints", "Физические\nпрепятствия", "calculate", 565, 125),
            node("rules", "Допустимые\nзоны", "calculate", 760, 125),
            node("database", "PostGIS: виды,\nправила, НПА", "calculate", 565, 320, kind="data"),
            node("planner", "Схемы и\nCP-SAT", "calculate", 760, 320),
            node("export", "DXF + отчёты", "deliver", 960, 125, kind="output"),
            node("verify", "Независимая\nпроверка", "deliver", 960, 320, kind="output"),
        ],
        "edges": [
            edge("dxf", "extract", "DXF"), edge("config", "extract"),
            edge("extract", "normalize", "объекты"), edge("extract", "model"),
            edge("model", "reconstruct", "принятые"),
            edge("normalize", "constraints"), edge("reconstruct", "constraints"),
            edge("constraints", "rules", "базовая зона"),
            edge("database", "rules", "отступы"), edge("database", "planner", "виды"),
            edge("rules", "planner", "зоны по типам"),
            edge("planner", "export", "план"), edge("planner", "verify"),
            edge("export", "verify", "итоговый DXF"),
        ],
        "note": "Сплошная стрелка — передача файла или результата расчёта. PostGIS поставляет справочники и правила.",
    },
    "process_flow": {
        "direction": "LR",
        "groups": [
            group("input", "Вход и масштаб", 20, 35, 200, 445),
            group("prepare", "Подготовка геометрии", 235, 35, 300, 445),
            group("decide", "Расчёт и решение", 550, 35, 290, 445),
            group("release", "Выпуск", 855, 35, 155, 445),
            group("bonus", "Визуализация", 1025, 35, 150, 445),
        ],
        "nodes": [
            node("source_dxf", "Исходный DXF", "input", 45, 105, 150, kind="source"),
            node("units", "Единицы и\nграница работ", "input", 45, 215, 150),
            node("input_config", "Карта слоёв и\nзапрос посадки", "input", 45, 325, 150, kind="source"),
            node("extract", "Go: объекты и\nповерхности", "prepare", 255, 105, 120),
            node("normalize", "Нормализация\nгеометрии", "prepare", 395, 105, 120),
            node("classify", "ONNX: очистка\nсетей", "prepare", 255, 300, 120, kind="model"),
            node("reconstruct", "Разрывы и\nврезки сетей", "prepare", 395, 300, 120),
            node("constraints", "Препятствия\nи дорога", "decide", 570, 105, 120),
            node("zones", "Зоны по видам\nпосадки", "decide", 700, 105, 120),
            node("zone_verify", "Проверка\nзон", "decide", 700, 205, 120),
            node("plan", "Схемы и\nCP-SAT", "decide", 570, 300, 120),
            node("checks", "Проверки и\nобъяснения", "decide", 700, 300, 120),
            node("result_dxf", "Новый DXF", "release", 870, 105, 125, kind="output"),
            node("verify", "Независимая\nпроверка", "release", 870, 230, 125),
            node("reports", "PDF, атлас,\nJSONL", "release", 870, 355, 125, kind="output"),
            node("scene", "Сцена и\nкамеры", "bonus", 1040, 105, 120),
            node("blender", "Blender:\nдо / после", "bonus", 1040, 230, 120, kind="output"),
            node("ai", "AI: материалы\nи свет", "bonus", 1040, 355, 120, kind="output"),
        ],
        "edges": [
            edge("source_dxf", "units"), edge("units", "extract"),
            edge("input_config", "extract"), edge("extract", "normalize"),
            edge("extract", "classify"), edge("classify", "reconstruct"),
            edge("normalize", "constraints"), edge("reconstruct", "constraints"),
            edge("constraints", "zones"), edge("zones", "zone_verify"),
            edge("zone_verify", "plan", "passed"),
            edge("input_config", "plan", "параметры",
                 via=[(210, 359), (210, 458), (545, 458), (545, 334)]),
            edge("plan", "checks"), edge("checks", "result_dxf", "принятые"),
            edge("result_dxf", "verify"), edge("verify", "reports", "passed"),
            edge("verify", "scene", "необязательно, отдельный запуск"),
            edge("scene", "blender"), edge("blender", "ai", "необязательно"),
        ],
        "note": "Визуализация запускается отдельно: Blender читает готовый план, AI при выборе обрабатывает кадры. DXF и координаты не изменяются.",
    },
    "utility_algorithm": {
        "direction": "LR",
        "groups": [
            group("input", "Графика", 20, 35, 175, 445),
            group("classify", "Модель", 210, 35, 405, 445),
            group("geometry", "Реконструкция", 630, 35, 335, 445),
            group("result", "Выход", 980, 35, 195, 445),
        ],
        "nodes": [
            node("raw", "Примитивы\nсетей", "input", 40, 195, kind="source"),
            node("features", "19 признаков\nгеометрии и графа", "classify", 230, 120, 150),
            node("onnx", "ONNX:\nвероятность", "classify", 435, 120, 150, kind="model"),
            node("accepted", "Принято", "classify", 435, 270, 150, kind="output"),
            node("review", "Спорно /\nотклонено", "classify", 230, 340, 150, kind="warning"),
            node("endpoints", "Свободные\nконцы", "geometry", 650, 120, 135),
            node("candidates", "Продолжение\nили врезка", "geometry", 810, 120, 135),
            node("selection", "Оценка\nсоединений", "geometry", 730, 300, 150),
            node("clean", "Оси сети +\nсоединители", "result", 1000, 120, 150, kind="output"),
            node("trace", "Гипотезы\nи причины", "result", 1000, 315, 150, kind="data"),
        ],
        "edges": [
            edge("raw", "features"), edge("features", "onnx"),
            edge("onnx", "accepted", "порог"), edge("onnx", "review"),
            edge("accepted", "endpoints", "кроме кабеля"),
            edge("endpoints", "candidates", "зазор + угол"),
            edge("candidates", "selection"), edge("selection", "clean"),
            edge("selection", "trace"), edge("accepted", "clean", "кабель: без достройки"),
        ],
        "note": "Модель определяет принадлежность линии сети. Соединитель создаётся только отдельным геометрическим этапом.",
    },
    "allow_zone_algorithm": {
        "direction": "LR",
        "groups": [
            group("source", "Геометрия подосновы", 20, 35, 230, 445),
            group("base", "Базовая зона", 265, 35, 310, 445),
            group("rules", "Правила по типам посадки", 590, 35, 355, 445),
            group("out", "Выход", 960, 35, 215, 445),
        ],
        "nodes": [
            node("boundary", "Граница работ", "source", 40, 105, 185, kind="source"),
            node("surfaces", "Подтверждённые\nповерхности", "source", 40, 220, 185, kind="source"),
            node("obstacles", "Препятствия:\nдорога, здания, люки", "source", 40, 335, 185, kind="source"),
            node("candidate", "Кандидат\nозеленения", "base", 290, 140, 150),
            node("subtract", "Вычесть\nпрепятствия", "base", 290, 315, 150),
            node("base_zone", "Базовая\nзона", "base", 465, 230, 90, kind="output"),
            node("db", "PostGIS:\nотступы и НПА", "rules", 610, 100, 145, kind="data"),
            node("networks", "Принятые\nлинии сетей", "rules", 610, 315, 145, kind="source"),
            node("buffers", "Буферы\nправил", "rules", 790, 200, 135),
            node("zones", "Зоны деревьев\nи кустарников", "out", 980, 135, 170, kind="output"),
            node("status", "Статусы правил\nи причины", "out", 980, 315, 170, kind="data"),
        ],
        "edges": [
            edge("boundary", "candidate"), edge("surfaces", "candidate"),
            edge("candidate", "subtract"), edge("obstacles", "subtract"),
            edge("subtract", "base_zone"), edge("base_zone", "buffers"),
            edge("db", "buffers", "порог"), edge("networks", "buffers", "геометрия"),
            edge("buffers", "zones", "вычитание"), edge("buffers", "status"),
        ],
        "note": "Если объект правила отсутствует, порог остаётся в отчёте; отсутствующая геометрия не образует нулевой буфер.",
    },
    "planting_algorithm": {
        "direction": "LR",
        "groups": [
            group("input", "Вход планировщика", 20, 35, 235, 445),
            group("design", "Композиция", 270, 35, 390, 445),
            group("check", "Контроль кандидата", 675, 35, 275, 445),
            group("out", "Решение", 965, 35, 210, 445),
        ],
        "nodes": [
            node("zone", "Зона по типу\nпосадки", "input", 40, 95, 190, kind="source"),
            node("catalog", "Каталог видов и\nрадиусов кроны", "input", 40, 215, 190, kind="data"),
            node("request", "Пресет или\nзапрос", "input", 40, 335, 190, kind="source"),
            node("species", "Вид и\nбезопасная зона", "design", 295, 110, 150),
            node("layout", "Варианты\nсхем", "design", 485, 110, 150),
            node("point", "CP-SAT:\nвыбор схем", "design", 485, 310, 150),
            node("area", "Кустарники\nи покров", "design", 295, 310, 150),
            node("checks", "Проверки по типу\nпосадки", "check", 700, 120, 205),
            node("reasons", "Правила и\nизмерения", "check", 700, 320, 205, kind="data"),
            node("accepted", "Принято:\nплан + DXF", "out", 990, 120, 160, kind="output"),
            node("rejected", "Отклонено:\nпричина + ID", "out", 990, 320, 160, kind="warning"),
        ],
        "edges": [
            edge("zone", "species"), edge("catalog", "species"),
            edge("request", "species"), edge("species", "layout"),
            edge("layout", "point"), edge("species", "area"),
            edge("point", "checks"), edge("area", "checks"),
            edge("checks", "accepted", "прошёл"),
            edge("checks", "rejected", "не прошёл"),
            edge("reasons", "checks", "пороги"),
        ],
        "note": "Решатель выбирает только из допустимых схем. Точка дерева и полигон кустарникового массива получают собственную проверку и ID.",
    },
    "deployment": {
        "direction": "LR",
        "groups": [
            group("operator", "Оператор", 20, 35, 235, 445),
            group("docker", "Docker Compose / хост Linux", 270, 35, 615, 445),
            group("files", "Файлы прогона", 900, 35, 275, 445),
        ],
        "nodes": [
            node("cli", "CLI:\nпараметры запуска", "operator", 45, 125, 185, kind="source"),
            node("seed", "seed: виды\nи правила", "docker", 295, 115, 160),
            node("postgis", "PostGIS", "docker", 515, 115, 150, kind="data"),
            node("planner", "planner:\nконвейер", "docker", 685, 270, 170),
            node("input", "/data/input.dxf", "files", 925, 100, 220, kind="source"),
            node("output", "/data/output:\nDXF, JSONL, PDF", "files", 925, 300, 220, kind="output"),
        ],
        "edges": [
            edge("seed", "postgis", "инициализация"),
            edge("postgis", "planner", "виды и правила"),
            edge("cli", "planner", "запуск",
                 via=[(245, 159), (245, 455), (675, 455), (675, 304)]),
            edge("input", "planner", "чтение"),
            edge("planner", "output", "запись"),
        ],
        "note": "seed и planner завершаются после работы. Итоговый DXF просматривают в CAD; HTTP API отсутствует.",
    },
}
