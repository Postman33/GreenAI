# Детектор осей инженерных сетей

Модуль обучается на копии DWG/DXF, где подтверждённые оси сетей имеют явный
цвет объекта `Green (ACI 3)`. Цвет используется только как обучающая метка.
Новые чертежи обрабатываются по геометрии и графу связности без ручной
раскраски.

## Обучение

```powershell
.\.tools\poetry\Scripts\poetry.exe run python .\utility_detector\detector.py train `
  ".\Пилотный проект 20 улиц\3. 3-я Парковая\Исходные данные\10001759_3-я Парковая ул\00_Ссылки\3_ДЖКХ-25_02294_labeled.dxf" `
  ".\XREF_ИГДИ_Камчатская улица_train.dxf" `
  ".\XREF_ИГДИ_Курганская_train.dxf" `
  --types gas_pipe,water_pipe `
  --model .\output\utility_detector_onnx `
  --report .\output\utility_detector_training_report.json `
  --debug-dxf .\output\utility_detector_training_debug.dxf `
  --debug-png .\output\utility_detector_training_debug.png
```

## Применение к JSONL экстрактора

```powershell
.\.tools\poetry\Scripts\poetry.exe run python .\utility_detector\detector.py predict `
  .\output\extracted_objects.jsonl `
  --model .\output\utility_detector_onnx `
  --output .\output\detected_utilities.geojsonl `
  --review-output .\output\possible_utilities.geojsonl `
  --rejected-output .\output\detected_annotations.geojsonl `
  --report .\output\utility_detector_prediction_report.json `
  --debug-dxf .\output\utility_detector_prediction_debug.dxf `
  --debug-png .\output\utility_detector_prediction_debug.png
```

Зелёные слои `ML_ACCEPTED_*` содержат обнаруженные оси, жёлтые
`ML_MANUAL_REVIEW_*` — неоднозначные примитивы, серые `ML_REJECTED_*` —
предполагаемую графику оформления. Модель из одного DWG имеет статус
`model_requires_cross_drawing_validation`: перед нормативным расчётом её нужно
проверить хотя бы на небольшом фрагменте другого DWG.

## Общий пайплайн

После обучения штатный сценарий автоматически использует bundle
`models/utility_detector/latest`:

```powershell
.\scripts\run_pipeline.ps1 `
  -InputDxf ".\Пилотный проект 20 улиц\input_10001759_bound.dxf" `
  -OutputDirectory ".\output_ml"
```

Параметр `-UtilityDetectorModel` нужен только для явного выбора другого ONNX
bundle. Если актуального bundle нет, сценарий использует консервативный
эвристический очиститель.
