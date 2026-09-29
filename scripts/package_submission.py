"""Build a submission bundle from a verified full run, without local secrets/caches."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from demo_preflight import check, read_json

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "README.md", "pyproject.toml", "poetry.lock", "requirements.txt",
    "go.mod", "go.sum", "Dockerfile", "docker-compose.yml", "init.sql",
    "init.ps1", "init.sh", ".gitignore", ".dockerignore", ".gitattributes",
    "inspect_dxf.py",
)
RUN_FILES = (
    "result_with_planting_plan.dxf", "planting_diagnostics.dxf",
    "sylvitect_planting_report.pdf", "planting_plan_atlas.pdf",
    "planting_plan_report.json", "verification_report.json",
    "pipeline_run_parameters.json", "pipeline_stage_timings.json",
    "pipeline_stage_timings.md", "planting_diagnostics_legend.md",
    "planting_area_schedule.json", "planting_explanations.md",
    "planting_plan.geojsonl", "planting_decisions.geojsonl",
    "planting_layout_trace.jsonl", "existing_tree_audit_report.json",
    "plant_allow_zones_report.json", "dxf_units_report.json",
)
EXTENSIONS = {".py", ".go", ".yaml", ".json", ".geojson", ".md", ".txt", ".sh", ".ps1"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sources() -> list[Path]:
    files = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for directory in ("src", "scripts", "parser", "tests", "examples"):
        files.extend(p for p in (ROOT / directory).rglob("*")
                     if p.is_file() and p.suffix in EXTENSIONS
                     and not {"__pycache__", "bin", "obj"}.intersection(p.parts))
    files.extend(p for p in (ROOT / "docs").rglob("*") if p.is_file()
                 and p.suffix in {".md", ".tex", ".png", ".svg"}
                 and p.name not in {"demo_audit.md", "DEMO_SCRIPT.md"})
    files.extend((ROOT / "models/utility_detector/latest").glob("*"))
    files.extend(p for p in (ROOT / "ml").iterdir() if p.is_file() and p.suffix in {".py", ".md"})
    files.extend((ROOT / "output/pdf").glob("*.pdf"))
    files.extend([ROOT / "config/planting.json", ROOT / "config/openai.env.example"])
    return sorted({p for p in files if p.is_file()})


def write_zip(path: Path, entries: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in entries:
            if source.suffix == ".sh":
                info = zipfile.ZipInfo.from_file(source, name)
                info.create_system = 3
                info.external_attr = 0o100755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, source.read_bytes().replace(b"\r\n", b"\n"))
            else:
                archive.write(source, name)
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Corrupt archive entry: {bad}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "output/submission_20260929")
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    problems = check(run)
    if problems:
        parser.error("; ".join(problems))
    input_path = Path(read_json(run / "pipeline_run_parameters.json")["input_dxf"])
    input_hash = digest(input_path)
    if input_hash != "a58369ba24b35b86be9ac70a20de4e81b6e49d78156692696a86e0eedfc855b3":
        parser.error("This demo package and its walkthrough require the verified 3rd Parkovaya input.")
    archive_path = output.with_suffix(".zip")
    if output.exists() or archive_path.exists():
        parser.error("Choose a new output path; existing submission packages are never overwritten.")
    presentation = ROOT / "Sylvitect-core_ЛЦТ2026_final.pptx"
    for required in [presentation, *(run / n for n in RUN_FILES),
                     ROOT / "output/pdf/Sylvitect-core_documentation.pdf",
                     ROOT / "output/pdf/sylvitect-core.pdf"]:
        if not required.is_file():
            parser.error(f"Missing artifact: {required}")
    output.mkdir(parents=True)
    for folder in ("source", "demo", "documentation", "presentation"):
        (output / folder).mkdir()
    for name in RUN_FILES:
        shutil.copy2(run / name, output / "demo" / name)
    shutil.copy2(presentation, output / "presentation" / presentation.name)
    for path in (ROOT / "output/pdf").glob("*.pdf"):
        shutil.copy2(path, output / "documentation" / path.name)
    for name in ("DEMO_WINDOWS.md", "DEMO_20260929.md", "RELEASE_CHECK.md"):
        shutil.copy2(ROOT / "docs" / name, output / "documentation" / name)

    source_files = sources()
    write_zip(output / "source/Sylvitect-core_source.zip",
              [(p, "Sylvitect-core/" + p.relative_to(ROOT).as_posix()) for p in source_files])
    (output / "START_HERE.md").write_text("""# Sylvitect-core · комплект решения

Вычислительное ядро формирования плана озеленения по DXF.

## Посмотреть результат

1. Открыть `demo/planting_plan_atlas.pdf`: страница 18 — участок У-17, подоснова, ограничения и посадки.
2. Открыть `demo/result_with_planting_plan.dxf` в CAD. Слои результата: `SYLVITECT_PLANT_TREE`, `SYLVITECT_PLANT_SHRUB`, `SYLVITECT_HERBACEOUS`.
3. Открыть `demo/sylvitect_planting_report.pdf`: паспорт T-0001 на странице 159, пример отказа R-S-0726 на странице 59. Для другого прогона искать ID по тексту отчёта.
4. Проверить `demo/verification_report.json` и `demo/planting_plan_report.json`.

Диагностический чертёж `demo/planting_diagnostics.dxf` содержит сети, зоны исключений, принятые посадки и отклонённые кандидаты. Легенда — `demo/planting_diagnostics_legend.md`.

## Документация и выступление

- `documentation/Sylvitect-core_documentation.pdf` — назначение, схемы, алгоритмы, запуск и ограничения.
- `documentation/sylvitect-core.pdf` — техническое описание с формулами.
- `documentation/DEMO_WINDOWS.md` — порядок показа и подсказки выступающему. Пути `output/submission_verification_20260929` в сценарии соответствуют папке `demo` этого комплекта.
- `documentation/RELEASE_CHECK.md` — фактически выполненные проверки и оставшаяся ручная проверка.
- `presentation/Sylvitect-core_ЛЦТ2026_final.pptx` — презентация.

## Воспроизвести расчёт

Распаковать `source/Sylvitect-core_source.zip`, открыть терминал в папке `Sylvitect-core`.
Запустить Docker Desktop на Windows либо Docker Engine на Linux.

Windows PowerShell:

```powershell
.\\init.ps1
.\\scripts\\run_pipeline.ps1 -InputDxf '.\\Пилотный проект 20 улиц\\input_10001759_bound.dxf' -OutputDirectory '.\\output\\demo' -PipelineMode full -PlantingPreset dense_mixed
.\\.venv\\Scripts\\python.exe scripts/demo_preflight.py output/demo
```

Linux:

```bash
bash init.sh
bash scripts/run_pipeline.sh 'Пилотный проект 20 улиц/input_10001759_bound.dxf' output/demo --mode full --preset dense_mixed
.venv-linux/bin/python scripts/demo_preflight.py output/demo
```

`init` устанавливает зависимости и скачивает демо-DXF с проверкой SHA-256. Нужен интернет; входной файл — около 348 МБ. Подробные требования и контейнерный вариант — `docs/quickstart.md` в исходниках. Для нового расчёта API-ключ и Blender не нужны. Приведённые команды не заказывают платные изображения.

Ключи API, виртуальное окружение, локальные кэши и остальные уличные чертежи в комплект не входят. Отчёты сохраняют абсолютные пути исходного запуска как сведения о происхождении; для просмотра используйте файлы в `demo`.

Контрольные суммы всех вложенных файлов перечислены в `SHA256SUMS.txt`; состав и результаты прогона — `manifest.json`.
""", encoding="utf-8")
    manifest = {
        "project": "Sylvitect-core", "created_at": datetime.now(timezone.utc).isoformat(),
        "run_directory": str(run), "source_file_count": len(source_files),
        "verification_status": read_json(run / "verification_report.json")["status"],
        "planting_summary": read_json(run / "planting_plan_report.json")["summary"],
        "elapsed_seconds": read_json(run / "pipeline_stage_timings.json")["total_elapsed_seconds"],
        "input_sha256": input_hash,
        "visualization": "No paid generation performed during release verification.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files = sorted(p for p in output.rglob("*") if p.is_file())
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{digest(p)}  {p.relative_to(output).as_posix()}\n" for p in files), encoding="utf-8")
    files = sorted(p for p in output.rglob("*") if p.is_file())
    write_zip(archive_path, [(p, output.name + "/" + p.relative_to(output).as_posix()) for p in files])
    archive_path.with_suffix(".zip.sha256").write_text(f"{digest(archive_path)}  {archive_path.name}\n", encoding="utf-8")
    print(f"Package: {archive_path} ({archive_path.stat().st_size / 1_000_000:.1f} MB)")


if __name__ == "__main__":
    main()
