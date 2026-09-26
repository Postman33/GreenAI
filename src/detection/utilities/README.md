# Р”РµС‚РµРєС‚РѕСЂ РѕСЃРµР№ РёРЅР¶РµРЅРµСЂРЅС‹С… СЃРµС‚РµР№

РњРѕРґСѓР»СЊ РѕР±СѓС‡Р°РµС‚СЃСЏ РЅР° РєРѕРїРёРё DWG/DXF, РіРґРµ РїРѕРґС‚РІРµСЂР¶РґС‘РЅРЅС‹Рµ РѕСЃРё СЃРµС‚РµР№ РёРјРµСЋС‚ СЏРІРЅС‹Р№
С†РІРµС‚ РѕР±СЉРµРєС‚Р° `Green (ACI 3)`. Р¦РІРµС‚ РёСЃРїРѕР»СЊР·СѓРµС‚СЃСЏ С‚РѕР»СЊРєРѕ РєР°Рє РѕР±СѓС‡Р°СЋС‰Р°СЏ РјРµС‚РєР°.
РќРѕРІС‹Рµ С‡РµСЂС‚РµР¶Рё РѕР±СЂР°Р±Р°С‚С‹РІР°СЋС‚СЃСЏ РїРѕ РіРµРѕРјРµС‚СЂРёРё Рё РіСЂР°С„Сѓ СЃРІСЏР·РЅРѕСЃС‚Рё Р±РµР· СЂСѓС‡РЅРѕР№
СЂР°СЃРєСЂР°СЃРєРё.

## РћР±СѓС‡РµРЅРёРµ

```powershell
..\.tools\poetry\Scripts\poetry.exe run python -m src.detection.utilities.detector train `
  ".\РџРёР»РѕС‚РЅС‹Р№ РїСЂРѕРµРєС‚ 20 СѓР»РёС†\3. 3-СЏ РџР°СЂРєРѕРІР°СЏ\РСЃС…РѕРґРЅС‹Рµ РґР°РЅРЅС‹Рµ\10001759_3-СЏ РџР°СЂРєРѕРІР°СЏ СѓР»\00_РЎСЃС‹Р»РєРё\3_Р”Р–РљРҐ-25_02294_labeled.dxf" `
  ".\XREF_РР“Р”Р_РљР°РјС‡Р°С‚СЃРєР°СЏ СѓР»РёС†Р°_train.dxf" `
  ".\XREF_РР“Р”Р_РљСѓСЂРіР°РЅСЃРєР°СЏ_train.dxf" `
  --types gas_pipe,water_pipe `
  --model .\output\utility_detector_onnx `
  --report .\output\utility_detector_training_report.json `
  --debug-dxf .\output\utility_detector_training_debug.dxf `
  --debug-png .\output\utility_detector_training_debug.png
```

## РџСЂРёРјРµРЅРµРЅРёРµ Рє JSONL СЌРєСЃС‚СЂР°РєС‚РѕСЂР°

```powershell
..\.tools\poetry\Scripts\poetry.exe run python -m src.detection.utilities.detector predict `
  .\output\extracted_objects.jsonl `
  --model .\output\utility_detector_onnx `
  --output .\output\detected_utilities.geojsonl `
  --review-output .\output\possible_utilities.geojsonl `
  --rejected-output .\output\detected_annotations.geojsonl `
  --report .\output\utility_detector_prediction_report.json `
  --debug-dxf .\output\utility_detector_prediction_debug.dxf `
  --debug-png .\output\utility_detector_prediction_debug.png
```

Р—РµР»С‘РЅС‹Рµ СЃР»РѕРё `ML_ACCEPTED_*` СЃРѕРґРµСЂР¶Р°С‚ РѕР±РЅР°СЂСѓР¶РµРЅРЅС‹Рµ РѕСЃРё, Р¶С‘Р»С‚С‹Рµ
`ML_MANUAL_REVIEW_*` вЂ” РЅРµРѕРґРЅРѕР·РЅР°С‡РЅС‹Рµ РїСЂРёРјРёС‚РёРІС‹, СЃРµСЂС‹Рµ `ML_REJECTED_*` вЂ”
РїСЂРµРґРїРѕР»Р°РіР°РµРјСѓСЋ РіСЂР°С„РёРєСѓ РѕС„РѕСЂРјР»РµРЅРёСЏ. РњРѕРґРµР»СЊ РёР· РѕРґРЅРѕРіРѕ DWG РёРјРµРµС‚ СЃС‚Р°С‚СѓСЃ
`model_requires_cross_drawing_validation`: РїРµСЂРµРґ РЅРѕСЂРјР°С‚РёРІРЅС‹Рј СЂР°СЃС‡С‘С‚РѕРј РµС‘ РЅСѓР¶РЅРѕ
РїСЂРѕРІРµСЂРёС‚СЊ С…РѕС‚СЏ Р±С‹ РЅР° РЅРµР±РѕР»СЊС€РѕРј С„СЂР°РіРјРµРЅС‚Рµ РґСЂСѓРіРѕРіРѕ DWG.

## РћР±С‰РёР№ РїР°Р№РїР»Р°Р№РЅ

РџРѕСЃР»Рµ РѕР±СѓС‡РµРЅРёСЏ С€С‚Р°С‚РЅС‹Р№ СЃС†РµРЅР°СЂРёР№ Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РёСЃРїРѕР»СЊР·СѓРµС‚ bundle
`models/utility_detector/latest`:

```powershell
.\scripts\run_pipeline.ps1 `
  -InputDxf ".\РџРёР»РѕС‚РЅС‹Р№ РїСЂРѕРµРєС‚ 20 СѓР»РёС†\input_10001759_bound.dxf" `
  -OutputDirectory ".\output_ml"
```

РџР°СЂР°РјРµС‚СЂ `-UtilityDetectorModel` РЅСѓР¶РµРЅ С‚РѕР»СЊРєРѕ РґР»СЏ СЏРІРЅРѕРіРѕ РІС‹Р±РѕСЂР° РґСЂСѓРіРѕРіРѕ ONNX
bundle. Р•СЃР»Рё Р°РєС‚СѓР°Р»СЊРЅРѕРіРѕ bundle РЅРµС‚, СЃС†РµРЅР°СЂРёР№ РёСЃРїРѕР»СЊР·СѓРµС‚ РєРѕРЅСЃРµСЂРІР°С‚РёРІРЅС‹Р№
СЌРІСЂРёСЃС‚РёС‡РµСЃРєРёР№ РѕС‡РёСЃС‚РёС‚РµР»СЊ.

