param(
    [switch]$DryRun,
    [string[]]$Answers
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pipeline = Join-Path $PSScriptRoot "run_pipeline.ps1"
$script:answerIndex = 0
$script:providedAnswers = $PSBoundParameters.ContainsKey("Answers")
$script:interactiveUi = $false
$script:notice = ""
$script:inputNotice = ""
$script:menuSelections = @{}
$script:lastRenderLines = 0
$script:lastWindowWidth = 0
$script:lastWindowHeight = 0

function Write-MenuLine {
    param(
        [string]$Text = "",
        [ConsoleColor]$Foreground = [ConsoleColor]::White,
        [ConsoleColor]$Background = [ConsoleColor]::DarkBlue
    )
    $width = [Math]::Max(1, [Console]::WindowWidth - 1)
    if ($Text.Length -gt $width) {
        $Text = if ($width -gt 1) { $Text.Substring(0, $width - 1) + "…" } else { "…" }
    }
    Write-Host $Text.PadRight($width) -ForegroundColor $Foreground -BackgroundColor $Background
    $script:renderLineCount++
}

function Read-MenuChoice {
    param([string]$Title, [string[]]$Keys, [string[]]$Labels, [string]$Subtitle = "")
    if (-not $script:interactiveUi) {
        Write-Host ""
        Write-Host $Title -ForegroundColor Cyan
        if ($Subtitle) { Write-Host $Subtitle }
        for ($i = 0; $i -lt $Keys.Count; $i++) {
            Write-Host "  $($Keys[$i]). $($Labels[$i])"
        }
        return Ask-Value "Выберите пункт"
    }

    $selected = 0
    if ($script:menuSelections.ContainsKey($Title)) {
        $selected = [Math]::Min($script:menuSelections[$Title], $Keys.Count - 1)
    }
    while ($true) {
        $width = [Console]::WindowWidth
        $height = [Console]::WindowHeight
        if ($height -lt 8) {
            [Console]::Clear()
            Write-Host "$Title — увеличьте высоту окна для экранного меню." -ForegroundColor Cyan
            for ($i = 0; $i -lt $Keys.Count; $i++) {
                Write-Host "  $($Keys[$i]). $($Labels[$i])"
            }
            return Read-Host "Выберите пункт"
        }
        if ($width -ne $script:lastWindowWidth -or $height -ne $script:lastWindowHeight) {
            [Console]::Clear()
            $script:lastRenderLines = 0
            $script:lastWindowWidth = $width
            $script:lastWindowHeight = $height
        }
        [Console]::SetCursorPosition(0, 0)
        $script:renderLineCount = 0
        $compact = $height -lt 16
        if (-not $compact) { Write-MenuLine }
        Write-MenuLine "  GreenAI  ·  проект озеленения" Cyan
        if (-not $compact) {
            Write-MenuLine ("  " + ("─" * [Math]::Min(58, [Math]::Max(1, $width - 5)))) DarkCyan
        }
        Write-MenuLine "  $Title" White
        if ($Subtitle -and -not $compact) { Write-MenuLine "  $Subtitle" Gray }
        Write-MenuLine

        $reserved = if ($compact) { 7 } else { 10 }
        $visible = [Math]::Max(1, [Math]::Min($Keys.Count, $height - $reserved))
        $first = [Math]::Max(0, [Math]::Min($selected - [int][Math]::Floor($visible / 2), $Keys.Count - $visible))
        for ($i = $first; $i -lt ($first + $visible); $i++) {
            $marker = if ($i -eq $selected) { "›" } else { " " }
            $line = "  $marker $($Keys[$i])  $($Labels[$i])"
            if ($i -eq $selected) {
                Write-MenuLine $line White DarkCyan
            } else {
                Write-MenuLine $line Gray DarkBlue
            }
        }
        Write-MenuLine
        if ($script:notice) {
            Write-MenuLine "  $script:notice" Yellow
        }
        $position = if ($visible -lt $Keys.Count) { "  $($selected + 1)/$($Keys.Count)" } else { "" }
        $hint = if ($compact) { "  ↑↓ выбор  Enter открыть  Esc назад$position" } else {
            "  ↑↓ выбор   Enter открыть   Esc назад   0–9 быстро$position"
        }
        Write-MenuLine $hint Cyan
        while ($script:renderLineCount -lt $script:lastRenderLines) { Write-MenuLine }
        $script:lastRenderLines = $script:renderLineCount
        $key = [Console]::ReadKey($true)
        switch ($key.Key) {
            "UpArrow" { $selected = ($selected - 1 + $Keys.Count) % $Keys.Count }
            "DownArrow" { $selected = ($selected + 1) % $Keys.Count }
            "Enter" {
                $script:menuSelections[$Title] = $selected
                $script:notice = ""
                return $Keys[$selected]
            }
            "Escape" { $script:notice = ""; return "0" }
            default {
                $digit = [string]$key.KeyChar
                if ($digit -in $Keys) {
                    $script:menuSelections[$Title] = [Array]::IndexOf($Keys, $digit)
                    $script:notice = ""
                    return $digit
                }
            }
        }
    }
}

function Restore-ConsoleTheme {
    if ($script:interactiveUi) {
        [Console]::ForegroundColor = $script:originalForeground
        [Console]::BackgroundColor = $script:originalBackground
        [Console]::Clear()
        $script:interactiveUi = $false
    }
}

function Ask-Value {
    param([string]$Prompt, [string]$Default = "")
    $label = if ($Default) { "$Prompt [$Default]" } else { $Prompt }
    if ($script:providedAnswers) {
        if ($script:answerIndex -ge $script:Answers.Count) {
            throw "Недостаточно ответов для запроса: $Prompt"
        }
        $answer = $script:Answers[$script:answerIndex]
        $script:answerIndex++
        Write-Host "$label`: $answer"
    } else {
        if ($script:interactiveUi) {
            [Console]::Clear()
            $script:lastRenderLines = 0
            $script:renderLineCount = 0
            Write-MenuLine
            Write-MenuLine "  GreenAI  ·  проект озеленения" Cyan
            Write-MenuLine "  Ввод параметра" White
            Write-MenuLine
            if ($script:inputNotice) {
                Write-MenuLine "  $script:inputNotice" Yellow
                Write-MenuLine
                $script:inputNotice = ""
            }
            $answer = Read-Host "  › $label"
        } else {
            $answer = Read-Host $label
        }
    }
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().Trim('"').Trim("'")
}

function Resolve-WorkspacePath {
    param([string]$Value)
    if ([IO.Path]::IsPathRooted($Value)) { return $Value }
    return Join-Path $workspace $Value
}

function Test-OutputFileLocked {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $stream = [IO.File]::Open(
            $Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
        $stream.Dispose()
        return $false
    } catch {
        return $true
    }
}

function Ask-ExistingPath {
    param([string]$Prompt, [string]$Default = "", [string]$Kind = "File")
    while ($true) {
        $value = Ask-Value $Prompt $Default
        if (-not $value) { return "" }
        $candidate = Resolve-WorkspacePath $value
        $valid = if ($Kind -eq "Directory") {
            Test-Path -LiteralPath $candidate -PathType Container
        } else {
            Test-Path -LiteralPath $candidate -PathType Leaf
        }
        if ($valid) { return (Resolve-Path -LiteralPath $candidate).Path }
        $script:inputNotice = "Файл или папка не найдены: $candidate"
        if (-not $script:interactiveUi) { Write-Host $script:inputNotice -ForegroundColor Yellow }
    }
}

function Ask-PositiveDouble {
    param([string]$Prompt)
    while ($true) {
        $value = Ask-Value "$Prompt (Enter — по умолчанию)"
        if (-not $value) { return $null }
        $parsed = 0.0
        $normalized = $value.Replace(",", ".")
        if (
            [double]::TryParse(
                $normalized,
                [Globalization.NumberStyles]::Float,
                [Globalization.CultureInfo]::InvariantCulture,
                [ref]$parsed
            ) -and $parsed -gt 0 -and -not [double]::IsInfinity($parsed)
        ) { return $parsed }
        $script:inputNotice = "Введите положительное число."
        if (-not $script:interactiveUi) { Write-Host $script:inputNotice -ForegroundColor Yellow }
    }
}

function Ask-PositiveInt {
    param([string]$Prompt)
    while ($true) {
        $value = Ask-Value "$Prompt (Enter — по умолчанию)"
        if (-not $value) { return $null }
        $parsed = 0
        if ([int]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) {
            return $parsed
        }
        $script:inputNotice = "Введите положительное целое число."
        if (-not $script:interactiveUi) { Write-Host $script:inputNotice -ForegroundColor Yellow }
    }
}

function Select-MenuOption {
    param([string]$Title, [string[]]$Values, [string[]]$Descriptions, [string]$Current)
    while ($true) {
        $keys = @()
        $labels = @()
        for ($i = 0; $i -lt $Values.Count; $i++) {
            $keys += [string]($i + 1)
            $mark = if ($Values[$i] -eq $Current) { "  ✓" } else { "" }
            $labels += "$($Descriptions[$i])$mark"
        }
        $keys += "0"
        $labels += "Назад"
        $choice = Read-MenuChoice $Title $keys $labels "Сейчас: $Current"
        if ($choice -eq "0") { return $Current }
        $index = 0
        if ([int]::TryParse($choice, [ref]$index) -and $index -ge 1 -and $index -le $Values.Count) {
            return $Values[$index - 1]
        }
        $script:notice = "Нет такого пункта меню."
        if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
    }
}

function Get-OutputPath {
    $defaultPath = Join-Path $workspace "output\latest"
    if (Test-Path -LiteralPath $defaultPath -PathType Leaf) {
        throw "Путь результата указывает на файл: $defaultPath"
    }
    $lockedOutputs = @(
        @("planting_overlay.dxf", "result_with_planting_plan.dxf", "planting_diagnostics.dxf") |
            Where-Object { Test-OutputFileLocked (Join-Path $defaultPath $_) }
    )
    if ($lockedOutputs.Count -eq 0) { return $defaultPath }

    $base = "run_" + (Get-Date -Format "yyyyMMdd_HHmmss")
    $parent = Join-Path $workspace "output"
    $candidate = Join-Path $parent $base
    $suffix = 2
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $parent "${base}_$suffix"
        $suffix++
    }
    Write-Host "Выходной DXF открыт или недоступен: $($lockedOutputs[0])" -ForegroundColor Yellow
    Write-Host "Результат будет сохранён в: $candidate"
    return $candidate
}

function Test-OpenAIKeyConfigured {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            if ($line -match '^\s*OPENAI_API_KEY\s*=\s*(.+?)\s*$') {
                $value = $Matches[1].Trim().Trim('"').Trim("'")
                return -not [string]::IsNullOrWhiteSpace($value)
            }
        }
    } catch {
        return $false
    }
    return $false
}

function Show-VisualizationMenu {
    if (-not (Test-Path -LiteralPath $script:defaultOpenAiKeyFile)) {
        $folder = Split-Path -Parent $script:defaultOpenAiKeyFile
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        [IO.File]::WriteAllText(
            $script:defaultOpenAiKeyFile,
            "OPENAI_API_KEY=`r`n",
            [Text.UTF8Encoding]::new($false)
        )
    }
    while ($true) {
        $keyStatus = if (Test-OpenAIKeyConfigured $script:openAiKeyFile) { "ключ указан" } else { "ключ не задан" }
        $keyName = if ($script:openAiKeyFile -eq $script:defaultOpenAiKeyFile) {
            "config\openai.env"
        } else {
            [IO.Path]::GetFileName($script:openAiKeyFile)
        }
        $labels = @(
            "Файл ключа OpenAI: $keyName ($keyStatus)",
            "Строить фотореалистичные изображения: выключено",
            "Назад"
        )
        switch (Read-MenuChoice "Фотореализм" @("1", "2", "0") $labels "Генерация изображений пока не подключена") {
            "1" {
                $candidate = Ask-ExistingPath "Файл с OPENAI_API_KEY (Enter — config\openai.env)"
                $script:openAiKeyFile = if ($candidate) { $candidate } else { $script:defaultOpenAiKeyFile }
            }
            "2" {
                $script:notice = "Генерация пока недоступна и не будет запущена."
                if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
            }
            "0" { return }
            default {
                $script:notice = "Нет такого пункта меню."
                if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
            }
        }
    }
}

function Show-SettingsMenu {
    while ($true) {
        $labels = @(
            "Проверять/запускать PostGIS: $(if ($script:startDatabase) { 'да' } else { 'нет' })",
            "Шаг между деревьями, м: $(if ($null -eq $script:spacing) { 'авто' } else { $script:spacing })",
            "Максимум деревьев: $(if ($null -eq $script:maxTrees) { 'авто' } else { $script:maxTrees })",
            "Единиц DXF на метр: $(if ($null -eq $script:units) { 'авто' } else { $script:units })",
            "Модель сетей: $(if ($script:model) { $script:model } else { 'стандартная' })",
            "Правки дорог: $(if ($script:corrections) { $script:corrections } else { 'авто' })",
            "Запрос на посадку: $(if ($script:request) { $script:request } else { 'выбранный стиль' })",
            "Назад"
        )
        switch (Read-MenuChoice "Настройки" @("1", "2", "3", "4", "5", "6", "7", "0") $labels "Пустой ввод сбрасывает значение") {
            "1" { $script:startDatabase = -not $script:startDatabase }
            "2" { $script:spacing = Ask-PositiveDouble "Шаг между деревьями, м" }
            "3" { $script:maxTrees = Ask-PositiveInt "Максимум деревьев" }
            "4" { $script:units = Ask-PositiveDouble "Единиц DXF на метр" }
            "5" { $script:model = Ask-ExistingPath "Папка модели (Enter — стандартная)" "" "Directory" }
            "6" { $script:corrections = Ask-ExistingPath "Файл правок дорог GeoJSON (Enter — авто)" }
            "7" { $script:request = Ask-ExistingPath "Запрос на посадку JSON (Enter — выбранный стиль)" }
            "0" { return }
            default {
                $script:notice = "Нет такого пункта меню."
                if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
            }
        }
    }
}

$inputDxf = ""
$mode = "auto"
$preset = "dense_mixed"
$script:startDatabase = $true
$script:spacing = $null
$script:maxTrees = $null
$script:units = $null
$script:model = ""
$script:corrections = ""
$script:request = ""
$script:defaultOpenAiKeyFile = Join-Path $workspace "config\openai.env"
$script:openAiKeyFile = $script:defaultOpenAiKeyFile

try {
    if (-not $script:providedAnswers -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
        $script:originalForeground = [Console]::ForegroundColor
        $script:originalBackground = [Console]::BackgroundColor
        try {
            [Console]::BackgroundColor = [ConsoleColor]::DarkBlue
            [Console]::ForegroundColor = [ConsoleColor]::White
            [Console]::Clear()
            $script:interactiveUi = $true
        } catch {
            [Console]::ForegroundColor = $script:originalForeground
            [Console]::BackgroundColor = $script:originalBackground
        }
    }

    if (-not $script:interactiveUi) {
        Write-Host "GreenAI — меню запуска пайплайна" -ForegroundColor Cyan
        Write-Host "Пути можно вводить целиком или относительно: $workspace"
    }

while ($true) {
    $dxfLabel = if ($inputDxf) { [IO.Path]::GetFileName($inputDxf) } else { "выбрать файл" }
    $modeLabel = switch ($mode) {
        "auto" { "Авто" }
        "lean" { "Лёгкий оверлей" }
        "full" { "Полный DXF" }
        "fast" { "Только кэш" }
    }
    $presetLabel = switch ($preset) {
        "dense_mixed" { "Плотная смешанная" }
        "balanced_mixed" { "Сбалансированная" }
        "tree_lawn" { "Деревья и газон" }
        "trees_only" { "Только деревья" }
        "shrub_lawn" { "Кустарники и газон" }
        "shrubs_only" { "Только кустарники" }
        "lawn_only" { "Только газон" }
    }
    $labels = @(
        "Исходный DXF      $dxfLabel",
        "Режим расчёта     $modeLabel",
        "Схема посадок     $presetLabel",
        "Доп. настройки",
        "Фотореализм       выключен",
        "Построить план    →",
        "Выход"
    )
    switch (Read-MenuChoice "Новый план посадок" @("1", "2", "3", "4", "5", "6", "0") $labels "Результат: output\latest") {
        "1" {
            $candidate = Ask-ExistingPath "Путь к исходному DXF (можно перетащить файл сюда)"
            if ($candidate) {
                if ([IO.Path]::GetExtension($candidate) -ieq ".dxf") {
                    $inputDxf = $candidate
                } else {
                    $script:notice = "Нужен экспортированный файл .dxf, а не DWG."
                    if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
                }
            }
        }
        "2" {
            $mode = Select-MenuOption "Режим" @("auto", "lean", "full", "fast") @(
                "Авто: полный первый запуск, затем кэш",
                "Лёгкий оверлей",
                "Всегда полный DXF",
                "Быстро: только при готовом кэше"
            ) $mode
        }
        "3" {
            $preset = Select-MenuOption "Стиль посадки" @(
                "dense_mixed", "balanced_mixed", "tree_lawn", "trees_only",
                "shrub_lawn", "shrubs_only", "lawn_only"
            ) @(
                "Плотная смешанная", "Сбалансированная смешанная",
                "Деревья и газон", "Только деревья", "Кустарники и газон",
                "Только кустарники", "Только газон"
            ) $preset
        }
        "4" { Show-SettingsMenu }
        "5" { Show-VisualizationMenu }
        "6" {
            if (-not $inputDxf) {
                $script:notice = "Сначала выберите исходный DXF (пункт 1)."
                if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
                continue
            }
            $outputPath = Get-OutputPath
            $options = @{
                InputDxf = $inputDxf
                OutputDirectory = $outputPath
                PipelineMode = $mode
                PlantingPreset = $preset
            }
            if (-not $script:startDatabase) { $options.SkipDatabaseStart = $true }
            if ($null -ne $script:spacing) { $options.TreeSpacingM = $script:spacing }
            if ($null -ne $script:maxTrees) { $options.TreeMaxCount = $script:maxTrees }
            if ($null -ne $script:units) { $options.DxfUnitsPerMeter = $script:units }
            if ($script:model) { $options.UtilityDetectorModel = $script:model }
            if ($script:corrections) { $options.RoadCorrections = $script:corrections }
            if ($script:request) { $options.PlantingRequest = $script:request }

            Restore-ConsoleTheme
            Write-Host ""
            Write-Host "Запуск:" -ForegroundColor Green
            foreach ($key in @(
                "InputDxf", "OutputDirectory", "PipelineMode", "PlantingPreset",
                "SkipDatabaseStart", "TreeSpacingM", "TreeMaxCount", "DxfUnitsPerMeter",
                "UtilityDetectorModel", "RoadCorrections", "PlantingRequest"
            )) {
                if ($options.ContainsKey($key)) { Write-Host "  $key = $($options[$key])" }
            }
            Write-Host "  Отладочный DXF = $(Join-Path $outputPath 'planting_diagnostics.dxf')"
            Write-Host "  Фотореалистичные изображения = выключены (OpenAI не вызывается)"
            if ($DryRun) {
                Write-Host "Проверка параметров завершена; пайплайн не запускался."
                return
            }
            Push-Location $workspace
            try {
                & $pipeline @options
            } finally {
                Pop-Location
            }
            return
        }
        "0" { return }
        default {
            $script:notice = "Нет такого пункта меню."
            if (-not $script:interactiveUi) { Write-Host $script:notice -ForegroundColor Yellow }
        }
    }
}
} finally {
    Restore-ConsoleTheme
}
