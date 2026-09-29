#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/scripts/run_pipeline.sh"
if [[ -f /data/input.dxf ]]; then
  INPUT=/data/input.dxf
  OUTPUT=/data/output
else
  INPUT="$ROOT/Пилотный проект 20 улиц/input_10001759_bound.dxf"
  OUTPUT="$ROOT/output/latest"
fi
MODE=full
PRESET=dense_mixed
UNITS=''
SPACING=''
MAX_TREES=''
MODEL=''
CORRECTIONS=''
REQUEST=''
SURVEY=''
KEY_FILE="$ROOT/config/openai.env"
PHOTO=0
SKIP_DATABASE=0

if [[ -n "${OPENROUTER_API_KEY:-}${OPENAI_API_KEY:-}" || -s "$KEY_FILE" ]] && \
   { [[ -x "${BLENDER_EXE:-/nonexistent}" ]] || command -v blender >/dev/null 2>&1; }; then
  PHOTO=1
fi

resolve_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then printf '%s' "$value"; else printf '%s' "$ROOT/$value"; fi
}

ask() {
  local label="$1" current="$2" answer
  printf '%s [%s]: ' "$label" "$current" >&2
  IFS= read -r answer || exit 0
  printf '%s' "${answer:-$current}"
}

choose() {
  local title="$1" current="$2" answer
  shift 2
  printf '\n%s\n' "$title" >&2
  local index=1 item
  for item in "$@"; do
    printf '  %d. %s%s\n' "$index" "$item" "$([[ "$item" == "$current" ]] && printf ' ✓')" >&2
    ((index+=1))
  done
  printf 'Выбор [%s]: ' "$current" >&2
  IFS= read -r answer || exit 0
  if [[ -z "$answer" ]]; then printf '%s' "$current"; return; fi
  if [[ "$answer" =~ ^[0-9]+$ ]] && ((answer >= 1 && answer <= $#)); then
    local options=("$@")
    printf '%s' "${options[answer-1]}"
  else
    printf 'Нет такого пункта. Сохранено: %s\n' "$current" >&2
    printf '%s' "$current"
  fi
}

build_command() {
  COMMAND=(bash "$RUNNER" "$INPUT" "$OUTPUT" "--mode" "$MODE")
  if [[ -n "$REQUEST" ]]; then
    COMMAND+=("--request" "$REQUEST")
  fi
  if [[ -n "$SURVEY" ]]; then COMMAND+=("--existing-shrub-survey" "$SURVEY"); fi
  if [[ -z "$REQUEST" ]]; then
    COMMAND+=("--preset" "$PRESET")
    if [[ -n "$SPACING" ]]; then COMMAND+=("--tree-spacing-m" "$SPACING"); fi
    if [[ -n "$MAX_TREES" ]]; then COMMAND+=("--tree-max-count" "$MAX_TREES"); fi
  fi
  if [[ -n "$UNITS" ]]; then COMMAND+=("--dxf-units-per-meter" "$UNITS"); fi
  if [[ -n "$MODEL" ]]; then COMMAND+=("--model" "$MODEL"); fi
  if [[ -n "$CORRECTIONS" ]]; then COMMAND+=("--road-corrections" "$CORRECTIONS"); fi
  if ((SKIP_DATABASE)); then COMMAND+=("--skip-database-start"); fi
}

run_plan() {
  if [[ ! -f "$INPUT" ]]; then
    printf 'DXF не найден: %s\n' "$INPUT" >&2
    return 1
  fi
  build_command
  local render_photo="$PHOTO"
  if ((render_photo)) && [[ ! -s "$KEY_FILE" && -z "${OPENROUTER_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
    printf 'Ключ OpenRouter не найден; расчёт продолжится без фотографий.\n' >&2
    render_photo=0
  fi
  if ((render_photo)) && [[ ! -x "${BLENDER_EXE:-/nonexistent}" ]] && ! command -v blender >/dev/null 2>&1; then
    printf 'Blender не найден; расчёт продолжится без фотографий. Установите его через scripts/install.sh --with-blender.\n' >&2
    render_photo=0
  fi
  printf '\nВход: %s\nРезультат: %s\nРежим: %s\nСтиль: %s\nФотореализм: %s\n' \
    "$INPUT" "$OUTPUT" "$MODE" "$PRESET" "$([[ "$render_photo" -eq 1 ]] && printf 'включён' || printf 'выключен')"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    "${COMMAND[@]}" --validate-only
    printf 'Проверка параметров завершена; расчёт и запросы изображений не запускались.\n'
    return
  fi
  "${COMMAND[@]}"
  if ((render_photo)); then
    local python
    if [[ -n "${SYLVITECT_PYTHON:-}" ]]; then
      python="$SYLVITECT_PYTHON"
    elif [[ -x "$ROOT/.venv-linux/bin/python" ]]; then
      python="$ROOT/.venv-linux/bin/python"
    else
      python="$(command -v python3 || command -v python)"
    fi
    local gallery_args=(
      "$ROOT/scripts/render_visualization_gallery.py"
      --pipeline-output "$OUTPUT" --places 1 --quality draft
    )
    if [[ -n "${BLENDER_EXE:-}" ]]; then gallery_args+=(--blender "$BLENDER_EXE"); fi
    "$python" "${gallery_args[@]}"
    local status=0
    "$python" "$ROOT/scripts/render_photorealistic_gallery.py" \
      --gallery-index "$OUTPUT/blender_gallery/gallery_index.json" \
      --key-file "$KEY_FILE" --places 1 --views both --max-images 4 --max-cost-usd 0.10 || status=$?
    if [[ "$status" -eq 2 ]]; then
      printf 'DXF готов. Фото приостановлены по лимиту расходов.\n'
    elif [[ "$status" -ne 0 ]]; then
      printf 'DXF готов, но генерация изображений завершилась с кодом %s.\n' "$status" >&2
      return "$status"
    fi
  fi
  printf 'Готово: %s\n' "$OUTPUT"
}

DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  -h|--help)
    printf 'Использование: scripts/run_pipeline_interactive.sh [--dry-run]\n'
    exit 0 ;;
  *) printf 'Неизвестный параметр: %s\n' "$1" >&2; exit 2 ;;
esac
if ((DRY_RUN)); then run_plan; exit; fi

while true; do
  printf '\033[44;37m  Sylvitect-core · проект озеленения на Linux  \033[0m\n'
  printf '  1. Исходный DXF: %s\n' "$INPUT"
  printf '  2. Режим: %s\n' "$MODE"
  printf '  3. Стиль посадки: %s\n' "$PRESET"
  printf '  4. Настройки расчёта\n'
  printf '  5. Визуализация: %s\n' "$([[ "$PHOTO" -eq 1 ]] && printf 'включена' || printf 'выключена')"
  printf '  6. Построить план\n'
  printf '  0. Выход\n'
  printf 'Пункт [6]: '
  IFS= read -r choice || exit 0
  case "${choice:-6}" in
    1)
      candidate="$(ask 'Путь к DXF' "$INPUT")"
      candidate="$(resolve_path "$candidate")"
      if [[ -f "$candidate" && "${candidate,,}" == *.dxf ]]; then INPUT="$candidate"
      else printf 'Нужен существующий DXF: %s\n' "$candidate" >&2; fi ;;
    2) MODE="$(choose 'Режим' "$MODE" auto full lean fast)" ;;
    3) PRESET="$(choose 'Стиль' "$PRESET" dense_mixed balanced_mixed tree_lawn trees_only shrub_lawn shrubs_only lawn_only alley hedge shrub_mass free_group mixed_flowerbed)" ;;
    4)
      printf 'Пустая строка оставляет значение; «-» сбрасывает необязательный параметр.\n'
      value="$(ask 'Единиц DXF на метр (авто — пусто)' "$UNITS")"; UNITS="${value/#-/}"
      value="$(ask 'Шаг деревьев, м' "$SPACING")"; SPACING="${value/#-/}"
      value="$(ask 'Максимум деревьев' "$MAX_TREES")"; MAX_TREES="${value/#-/}"
      value="$(ask 'Файл запроса на посадку' "$REQUEST")"; REQUEST="${value/#-/}"
      value="$(ask 'Инвентаризация кустарников' "$SURVEY")"; SURVEY="${value/#-/}"
      value="$(ask 'Папка модели сетей' "$MODEL")"; MODEL="${value/#-/}"
      value="$(ask 'Правки дороги GeoJSON' "$CORRECTIONS")"; CORRECTIONS="${value/#-/}"
      value="$(ask 'Папка результата' "$OUTPUT")"; OUTPUT="$(resolve_path "$value")"
      option="$(choose 'PostGIS' "$([[ "$SKIP_DATABASE" -eq 1 ]] && printf 'не запускать' || printf 'проверять')" 'проверять' 'не запускать')"
      if [[ "$option" == 'не запускать' ]]; then SKIP_DATABASE=1; else SKIP_DATABASE=0; fi ;;
    5)
      option="$(choose 'Фотореализм OpenRouter' "$([[ "$PHOTO" -eq 1 ]] && printf 'включён' || printf 'выключен')" 'включён' 'выключен')"
      if [[ "$option" == 'включён' ]]; then PHOTO=1; else PHOTO=0; fi
      value="$(ask 'Файл ключа' "$KEY_FILE")"; KEY_FILE="$(resolve_path "$value")" ;;
    6) run_plan; exit ;;
    0) exit 0 ;;
    *) printf 'Нет такого пункта.\n' >&2 ;;
  esac
done
