using System.Drawing;
using System.Windows.Forms;
using HostMgd.Windows;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

internal static class GreenAiPanel
{
    private static readonly Guid PaletteId = new("86812B61-5F99-45EF-BD5A-BB18303CA84C");
    private static PaletteSet? _palette;
    private static GreenAiEditorControl? _control;

    public static void Show()
    {
        if (_palette is null)
        {
            _control = new GreenAiEditorControl();
            _palette = new PaletteSet("GreenAI — редактор озеленения", PaletteId)
            {
                Style = PaletteSetStyles.ShowAutoHideButton |
                        PaletteSetStyles.ShowCloseButton |
                        PaletteSetStyles.ShowPropertiesMenu,
                MinimumSize = new Size(340, 560),
                Size = new Size(390, 720),
                DockEnabled = DockSides.Left | DockSides.Right
            };
            _palette.Add("Озеленение", _control);
            _palette.Dock = DockSides.Right;
        }
        _palette.Visible = true;
    }

    public static void Close()
    {
        if (_palette is not null)
            _palette.Visible = false;
    }

    public static void Log(string message, bool error = false) => _control?.AddLog(message, error);
    public static void SetStatus(string text) => _control?.SetStatus(text);
    public static void PreviewUnavailable() => _control?.PreviewUnavailable();
    public static void ScopeUnavailable() => _control?.ScopeUnavailable();
    public static void ResultLayersShown() => _control?.ResultLayersShown();

    public static void ZoneSelected(string layer, string handle, double area)
    {
        _control?.SetZone(layer, handle, area);
        Log($"Выбрана зона на слое «{layer}», площадь {area:F1} кв. ед.");
    }

    public static void GuideSelected(string layer, string handle, double length)
    {
        _control?.SetGuide(layer, handle, length);
        Log($"Выбрана линия посадки на слое «{layer}», длина {length:F1} ед.");
    }

    public static void PreviewReady(
        int treeCount,
        int shrubCount,
        double areaSquareM,
        double selectedAreaSquareM) =>
        _control?.PreviewReady(treeCount, shrubCount, areaSquareM, selectedAreaSquareM);

    public static void GuidePreviewReady(int treeCount, int shrubCount, double lengthM, double spacingM) =>
        _control?.GuidePreviewReady(treeCount, shrubCount, lengthM, spacingM);

    public static void PlanReady(
        int treeCount,
        int shrubCount,
        double areaSquareM,
        string scenario,
        int warningCount,
        int manualReviewCount) =>
        _control?.PlanReady(
            treeCount, shrubCount, areaSquareM, scenario, warningCount, manualReviewCount);

    public static void ShowPassport(string title, IEnumerable<string> lines, bool error)
    {
        var text = string.Join(Environment.NewLine, lines);
        if (_control is not null)
            _control.ShowPassport(title, text, error);
        MessageBox.Show(
            text,
            title,
            MessageBoxButtons.OK,
            error ? MessageBoxIcon.Error : MessageBoxIcon.Information);
    }

    public static void ShowDetails(string title, IEnumerable<string> lines, bool error)
    {
        var text = string.Join(Environment.NewLine, lines);
        if (_control is not null && _palette?.Visible == true)
            _control.ShowDetails(title, text, error);
        else
            MessageBox.Show(
                text,
                title,
                MessageBoxButtons.OK,
                error ? MessageBoxIcon.Error : MessageBoxIcon.Information);
    }
}

internal sealed class GreenAiEditorControl : UserControl
{
    private readonly Label _zoneValue = new();
    private readonly ComboBox _plant = new();
    private readonly ComboBox _pattern = new();
    private readonly NumericUpDown _spacing = new();
    private readonly NumericUpDown _count = new();
    private readonly Button _applyButton = new();
    private readonly Button _previewButton = new();
    private readonly CheckedListBox _layers = new();
    private readonly ListBox _log = new();
    private readonly Label _status = new();
    private readonly Label _quickSummary = new();
    private readonly Button _quickRun = new();
    private readonly ComboBox _scenario = new();
    private readonly ProgressBar _progress = new();
    private readonly RichTextBox _passport = new();
    private readonly Button _manualButton = new();
    private bool _suppressLayerChanges;

    public GreenAiEditorControl()
    {
        Dock = DockStyle.Fill;
        AutoScroll = true;
        Font = new Font("Segoe UI", 9F);
        BackColor = SystemColors.Control;

        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(8),
            ColumnCount = 1,
            RowCount = 10,
            AutoScroll = true
        };
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        root.Controls.Add(BuildHeader(), 0, 0);
        root.Controls.Add(BuildQuickStartGroup(), 0, 1);
        root.Controls.Add(BuildZoneGroup(), 0, 2);
        root.Controls.Add(BuildPlantingGroup(), 0, 3);
        root.Controls.Add(BuildActionsGroup(), 0, 4);
        root.Controls.Add(BuildObjectEditorGroup(), 0, 5);
        root.Controls.Add(BuildPassportGroup(), 0, 6);
        root.Controls.Add(BuildLayersGroup(), 0, 7);
        root.Controls.Add(BuildLogGroup(), 0, 8);
        _status.Text = "Готово. Для начала нажмите «Создать план озеленения».";
        _status.Dock = DockStyle.Fill;
        _status.AutoSize = true;
        _status.Padding = new Padding(4, 5, 4, 2);
        root.Controls.Add(_status, 0, 9);
        Controls.Add(root);

        SyncOptions();
    }

    private Control BuildQuickStartGroup()
    {
        var group = NewGroup("Быстрый старт", 282);
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 7,
            Padding = new Padding(2)
        };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 22));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 12));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var instruction = new Label
        {
            Dock = DockStyle.Fill,
            Text = "Плагин сам использует границу работ, дороги, здания и инженерные сети.",
            ForeColor = SystemColors.GrayText
        };
        var scenarioLabel = new Label
        {
            Dock = DockStyle.Fill,
            Text = "Цель плана",
            TextAlign = ContentAlignment.BottomLeft
        };
        _scenario.Dock = DockStyle.Fill;
        _quickRun.Text = "СОЗДАТЬ ПЛАН ОЗЕЛЕНЕНИЯ";
        _quickRun.Dock = DockStyle.Fill;
        _quickRun.Font = new Font("Segoe UI", 10F, FontStyle.Bold);
        _quickRun.BackColor = Color.FromArgb(49, 128, 73);
        _quickRun.ForeColor = Color.White;
        _quickRun.FlatStyle = FlatStyle.Flat;
        _quickRun.Click += (_, _) => RunDirect(
            "Строю план для всей территории…",
            () => new Commands().Run());
        var dataQuality = NewButton("Проверить исходные данные и ограничения");
        ConfigureDirectAction(dataQuality, "Проверяю исходные данные…", () => new Commands().DataQuality());
        _progress.Dock = DockStyle.Fill;
        _progress.Style = ProgressBarStyle.Marquee;
        _progress.MarqueeAnimationSpeed = 25;
        _progress.Visible = false;
        _quickSummary.Dock = DockStyle.Fill;
        _quickSummary.Padding = new Padding(2, 5, 2, 2);
        _quickSummary.Text = "Результат ещё не построен";
        layout.Controls.Add(instruction, 0, 0);
        layout.Controls.Add(scenarioLabel, 0, 1);
        layout.Controls.Add(_scenario, 0, 2);
        layout.Controls.Add(_quickRun, 0, 3);
        layout.Controls.Add(dataQuality, 0, 4);
        layout.Controls.Add(_progress, 0, 5);
        layout.Controls.Add(_quickSummary, 0, 6);
        group.Controls.Add(layout);
        return group;
    }

    private Control BuildHeader()
    {
        var panel = new Panel { Dock = DockStyle.Top, Height = 54 };
        panel.Controls.Add(new Label
        {
            Dock = DockStyle.Fill,
            Text = "GreenAI v1.0\r\nИнтерактивный план озеленения",
            Font = new Font("Segoe UI", 11F, FontStyle.Bold),
            TextAlign = ContentAlignment.MiddleLeft
        });
        return panel;
    }

    private Control BuildZoneGroup()
    {
        var group = NewGroup("1. Где строить план", 180);
        var choose = NewButton("Выбрать участок (замкнутый контур)");
        choose.Dock = DockStyle.Top;
        choose.Height = 34;
        choose.Click += (_, _) =>
        {
            if (_pattern.SelectedItem is PatternChoice pattern && pattern.Code == "guide")
                SelectPattern("grid");
            RunCadCommand("GREENAI_SELECT_ZONE", "Выберите контур зоны на чертеже…");
        };
        var chooseGuide = NewButton("Выбрать линию посадки (LINE / POLYLINE)");
        chooseGuide.Dock = DockStyle.Top;
        chooseGuide.Height = 34;
        chooseGuide.Click += (_, _) =>
        {
            if (_plant.SelectedItem is PlantChoice plant && plant.IsMixed)
                _plant.SelectedIndex = 1;
            SelectPattern("guide");
            RunCadCommand("GREENAI_SELECT_GUIDE", "Выберите проектную ось ряда на чертеже…");
        };
        _zoneValue.Dock = DockStyle.Fill;
        _zoneValue.Text = "Зона или линия посадки не выбрана";
        _zoneValue.Padding = new Padding(2, 6, 2, 2);
        _zoneValue.AutoEllipsis = true;
        var replacementHint = new Label
        {
            Dock = DockStyle.Bottom,
            Height = 34,
            Text = "При добавлении старые посадки GreenAI внутри выбранного участка заменяются.",
            ForeColor = SystemColors.GrayText
        };
        group.Controls.Add(_zoneValue);
        group.Controls.Add(replacementHint);
        group.Controls.Add(chooseGuide);
        group.Controls.Add(choose);
        return group;
    }

    private Control BuildPlantingGroup()
    {
        var group = NewGroup("2. Настройте план", 170);
        var grid = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 4,
            Padding = new Padding(2)
        };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42F));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58F));
        AddField(grid, 0, "Растение", _plant);
        AddField(grid, 1, "Схема", _pattern);
        _spacing.DecimalPlaces = 1;
        _spacing.Minimum = 0.5M;
        _spacing.Maximum = 100M;
        _spacing.Increment = 0.5M;
        AddField(grid, 2, "Шаг, м", _spacing);
        _count.Minimum = 1;
        _count.Maximum = 5000;
        AddField(grid, 3, "Максимум", _count);
        group.Controls.Add(grid);
        return group;
    }

    private Control BuildActionsGroup()
    {
        var group = NewGroup("3. Постройте и добавьте результат", 178);
        var grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 3 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50F));
        _previewButton.Text = "Предпросмотр";
        _applyButton.Text = "Добавить в чертёж";
        _manualButton.Text = "Указать точку вручную";
        var check = NewButton("Проверить все посадки");
        var zoom = NewButton("Показать результат на чертеже");
        var clear = NewButton("Очистить результат");
        ConfigureDirectAction(_previewButton, "Строю предпросмотр…", () => new Commands().Preview());
        ConfigureDirectAction(_applyButton, "Добавляю посадки…", () => new Commands().ApplyPreview());
        ConfigureAction(_manualButton, "GREENAI_MANUAL", "Укажите точку посадки на чертеже…");
        ConfigureDirectAction(check, "Проверяю посадки…", () => new Commands().Check());
        ConfigureDirectAction(zoom, "Перехожу к результату…", () => new Commands().ZoomResult());
        ConfigureDirectAction(clear, "Очищаю результат…", () => new Commands().Clear());
        _applyButton.Enabled = false;
        _previewButton.Enabled = false;
        grid.Controls.Add(_previewButton, 0, 0);
        grid.Controls.Add(_applyButton, 1, 0);
        grid.Controls.Add(_manualButton, 0, 1);
        grid.Controls.Add(check, 1, 1);
        grid.Controls.Add(zoom, 0, 2);
        grid.Controls.Add(clear, 1, 2);
        group.Controls.Add(grid);
        return group;
    }

    private Control BuildObjectEditorGroup()
    {
        var group = NewGroup("Редактор отдельной посадки", 96);
        var hint = new Label
        {
            Text = "Выберите дерево или кустарник прямо на чертеже.",
            Dock = DockStyle.Top,
            Height = 25,
            ForeColor = SystemColors.GrayText
        };
        var buttons = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1 };
        buttons.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 34F));
        buttons.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33F));
        buttons.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33F));
        var inspect = NewButton("Информация");
        var move = NewButton("Перенести");
        var delete = NewButton("Удалить");
        ConfigureAction(inspect, "GREENAI_INSPECT", "Выберите посадку для просмотра паспорта…");
        ConfigureAction(move, "GREENAI_MOVE_PLANT", "Выберите посадку и укажите новое место…");
        ConfigureAction(delete, "GREENAI_DELETE_PLANT", "Выберите посадку для удаления…");
        buttons.Controls.Add(inspect, 0, 0);
        buttons.Controls.Add(move, 1, 0);
        buttons.Controls.Add(delete, 2, 0);
        group.Controls.Add(buttons);
        group.Controls.Add(hint);
        return group;
    }

    private Control BuildPassportGroup()
    {
        var group = NewGroup("Паспорт выбранной посадки", 185);
        _passport.Dock = DockStyle.Fill;
        _passport.ReadOnly = true;
        _passport.BorderStyle = BorderStyle.FixedSingle;
        _passport.BackColor = Color.White;
        _passport.Text = "Выберите «Информация», затем укажите посадку на чертеже.";
        group.Controls.Add(_passport);
        return group;
    }

    private Control BuildLayersGroup()
    {
        var group = NewGroup("4. Слои результата", 142);
        var autoCheck = new CheckBox
        {
            Text = "Автопроверка после перемещения",
            Dock = DockStyle.Bottom,
            Height = 25,
            Checked = true
        };
        autoCheck.CheckedChanged += (_, _) =>
        {
            GreenAiSession.AutoValidateEdits = autoCheck.Checked;
            AddLog($"Автопроверка: {(autoCheck.Checked ? "включена" : "выключена")}", false);
        };
        _layers.Dock = DockStyle.Fill;
        _layers.CheckOnClick = true;
        _layers.Items.Add("Деревья", true);
        _layers.Items.Add("Кустарники", true);
        _layers.Items.Add("Травянистое покрытие", true);
        _layers.Items.Add("Предпросмотр", true);
        _layers.Items.Add("Ошибки", true);
        _layers.ItemCheck += (_, args) =>
        {
            if (!_suppressLayerChanges)
                BeginInvoke(() => SetLayerVisibility(args.Index, args.NewValue == CheckState.Checked));
        };
        group.Controls.Add(_layers);
        group.Controls.Add(autoCheck);
        return group;
    }

    private Control BuildLogGroup()
    {
        var group = NewGroup("Ход работы и причины ограничений", 150);
        group.Dock = DockStyle.Fill;
        _log.Dock = DockStyle.Fill;
        _log.HorizontalScrollbar = true;
        group.Controls.Add(_log);
        return group;
    }

    private static GroupBox NewGroup(string title, int height) => new()
    {
        Text = title,
        Dock = DockStyle.Top,
        Height = height,
        Padding = new Padding(8)
    };

    private static Button NewButton(string text) => new()
    {
        Text = text,
        Dock = DockStyle.Fill,
        Margin = new Padding(3),
        FlatStyle = FlatStyle.System
    };

    private static void AddField(TableLayoutPanel grid, int row, string label, Control control)
    {
        grid.RowStyles.Add(new RowStyle(SizeType.Percent, 25F));
        grid.Controls.Add(new Label
        {
            Text = label,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft
        }, 0, row);
        control.Dock = DockStyle.Fill;
        grid.Controls.Add(control, 1, row);
    }

    private void ConfigureAction(Button button, string command, string status)
    {
        button.Dock = DockStyle.Fill;
        button.Margin = new Padding(3);
        button.Click += (_, _) => RunCadCommand(command, status);
    }

    private void ConfigureDirectAction(Button button, string status, Action action)
    {
        button.Dock = DockStyle.Fill;
        button.Margin = new Padding(3);
        button.Click += (_, _) => RunDirect(status, action);
    }

    private void SyncOptions()
    {
        _scenario.DropDownStyle = ComboBoxStyle.DropDownList;
        _scenario.Items.Add(new ScenarioChoice(
            "balanced", "Сбалансированный: деревья + кустарники + газон"));
        _scenario.Items.Add(new ScenarioChoice(
            "dense_balanced", "Плотный смешанный: больше деревьев + кустарники по краям + газон"));
        _scenario.Items.Add(new ScenarioChoice(
            "maximum_trees", "Максимум деревьев и будущей кроны"));
        _scenario.Items.Add(new ScenarioChoice(
            "maximum_plants", "Максимум количества растений"));
        _scenario.Items.Add(new ScenarioChoice(
            "grass_only", "Только газон / травянистое покрытие"));
        _scenario.SelectedIndex = 1;

        _plant.DropDownStyle = ComboBoxStyle.DropDownList;
        _plant.Items.Add(new PlantChoice(
            "mixed", "Смешанный план: деревья + кустарники + газон", 5M, 0.5M, false, true));
        _plant.Items.Add(new PlantChoice(
            "tree", "Дерево — липа; расчётная крона Ø 5 м", 5M, 5M));
        _plant.Items.Add(new PlantChoice(
            "shrub", "Кустарник — спирея; расчётная крона Ø 1,5 м", 2M, 1.5M));
        _plant.Items.Add(new PlantChoice(
            "herbaceous", "Газон / травянистое покрытие", 0.5M, 0.5M, true));
        _plant.SelectedIndexChanged += (_, _) =>
        {
            if (_plant.SelectedItem is PlantChoice choice)
            {
                _spacing.Minimum = choice.MinimumSpacing;
                _spacing.Value = choice.DefaultSpacing;
                _spacing.Enabled = !choice.IsArea && !choice.IsMixed;
                _count.Enabled = !choice.IsArea;
                _pattern.Enabled = !choice.IsArea;
                _manualButton.Enabled = !choice.IsArea && !choice.IsMixed;
            }
            InvalidatePreview("Растение изменено — обновите предпросмотр.");
        };
        _plant.SelectedIndex = 0;

        _pattern.DropDownStyle = ComboBoxStyle.DropDownList;
        _pattern.Items.Add(new PatternChoice("grid", "Сетка"));
        _pattern.Items.Add(new PatternChoice("boundary", "Вдоль границы"));
        _pattern.Items.Add(new PatternChoice("guide", "Вдоль выбранной линии"));
        _pattern.Items.Add(new PatternChoice("manual", "Ручная установка"));
        _pattern.SelectedIndex = 0;
        _spacing.Value = 5M;
        _count.Value = 5000M;
        _pattern.SelectedIndexChanged += (_, _) =>
            InvalidatePreview("Схема изменена — обновите предпросмотр.");
        _spacing.ValueChanged += (_, _) =>
            InvalidatePreview("Шаг изменён — обновите предпросмотр.");
        _count.ValueChanged += (_, _) =>
            InvalidatePreview("Максимальное количество изменено — обновите предпросмотр.");
    }

    private void InvalidatePreview(string message)
    {
        var hadPreview = GreenAiSession.PreviewContext is not null ||
                         GreenAiSession.Preview.Count > 0 || GreenAiSession.PreviewAreas.Count > 0;
        GreenAiSession.ResetPreview();
        _applyButton.Enabled = false;
        if (!hadPreview)
            return;
        try
        {
            Commands.ClearPreviewGraphics();
        }
        catch (Exception error)
        {
            AddLog($"Не удалось убрать устаревший предпросмотр: {error.Message}", true);
        }
        _quickSummary.Text = message;
        SetStatus(message);
    }

    private void SelectPattern(string code)
    {
        for (var index = 0; index < _pattern.Items.Count; index++)
        {
            if (_pattern.Items[index] is not PatternChoice item || item.Code != code)
                continue;
            _pattern.SelectedIndex = index;
            return;
        }
    }

    private void CaptureOptions()
    {
        if (_scenario.SelectedItem is ScenarioChoice scenario)
            GreenAiSession.Scenario = scenario.Code;
        if (_plant.SelectedItem is PlantChoice plant)
            GreenAiSession.PlantType = plant.Code;
        if (_pattern.SelectedItem is PatternChoice pattern)
            GreenAiSession.Pattern = pattern.Code;
        GreenAiSession.SpacingM = (double)_spacing.Value;
        GreenAiSession.MaxCount = (int)_count.Value;
    }

    private void RunCadCommand(string command, string status)
    {
        CaptureOptions();
        SetStatus(status);
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        document.SendStringToExecute(command + " ", true, false, false);
    }

    private void RunDirect(string status, Action action)
    {
        CaptureOptions();
        SetStatus(status);
        AddLog(status, false);
        _quickRun.Enabled = false;
        _progress.Visible = true;
        UseWaitCursor = true;
        Refresh();
        System.Windows.Forms.Application.DoEvents();
        try
        {
            action();
        }
        catch (Exception error)
        {
            ShowDetails("Ошибка GreenAI", error.Message, true);
        }
        finally
        {
            UseWaitCursor = false;
            _progress.Visible = false;
            _quickRun.Enabled = true;
        }
    }

    private void SetLayerVisibility(int index, bool visible)
    {
        var layer = index switch
        {
            0 => "GREEN_AI_PLANT_TREE",
            1 => "GREEN_AI_PLANT_SHRUB",
            2 => "GREEN_AI_HERBACEOUS",
            3 => "GREEN_AI_PREVIEW",
            _ => "GREEN_AI_REVIEW"
        };
        try
        {
            Commands.SetLayerVisibility(layer, visible);
            AddLog($"Слой «{_layers.Items[index]}»: {(visible ? "включён" : "выключен")}", false);
        }
        catch (Exception error)
        {
            AddLog($"Не удалось изменить слой: {error.Message}", true);
        }
    }

    public void SetZone(string layer, string handle, double area)
    {
        if (_pattern.SelectedItem is PatternChoice pattern && pattern.Code == "guide")
            SelectPattern("grid");
        _zoneValue.Text = $"Слой: {layer}; объект: {handle}; площадь: {area:F1}";
        _applyButton.Enabled = false;
        _previewButton.Enabled = true;
    }

    public void SetGuide(string layer, string handle, double length)
    {
        SelectPattern("guide");
        _zoneValue.Text = $"Линия: слой {layer}; объект: {handle}; длина: {length:F1}";
        _applyButton.Enabled = false;
        _previewButton.Enabled = true;
    }

    public void PreviewUnavailable() => _applyButton.Enabled = false;

    public void ScopeUnavailable()
    {
        _zoneValue.Text = "Зона или линия посадки не выбрана";
        _previewButton.Enabled = false;
        _applyButton.Enabled = false;
    }

    public void ResultLayersShown()
    {
        _suppressLayerChanges = true;
        try
        {
            for (var index = 0; index < _layers.Items.Count; index++)
                _layers.SetItemChecked(index, true);
        }
        finally
        {
            _suppressLayerChanges = false;
        }
    }

    public void PreviewReady(
        int treeCount,
        int shrubCount,
        double areaSquareM,
        double selectedAreaSquareM)
    {
        var count = treeCount + shrubCount;
        _applyButton.Enabled = count > 0 || areaSquareM > 0;
        var coveragePercent = selectedAreaSquareM > 0
            ? Math.Min(100.0, areaSquareM / selectedAreaSquareM * 100.0)
            : 0.0;
        _quickSummary.Text =
            $"Предпросмотр: деревьев — {treeCount}, кустарников — {shrubCount}; " +
            $"допустимое покрытие — {areaSquareM:F1} из {selectedAreaSquareM:F1} м² " +
            $"выбранного контура ({coveragePercent:F0}%).";
        SetStatus(
            $"Предпросмотр готов: {count} точечных посадок, покрытие {areaSquareM:F1} м². " +
            "Оставшаяся площадь исключена ограничениями или недостаточной шириной.");
    }

    public void GuidePreviewReady(int treeCount, int shrubCount, double lengthM, double spacingM)
    {
        var count = treeCount + shrubCount;
        _applyButton.Enabled = count > 0;
        _quickSummary.Text =
            $"Ряд вдоль линии: деревьев — {treeCount}, кустарников — {shrubCount}; " +
            $"длина оси — {lengthM:F1} м, проектный шаг — {spacingM:F1} м.";
        SetStatus(count > 0
            ? $"Предпросмотр ровного ряда готов: {count} посадок. Недопустимые точки автоматически исключены."
            : "На выбранной линии нет допустимых точек: весь ряд попал в ограничения.");
    }

    public void PlanReady(
        int treeCount,
        int shrubCount,
        double areaSquareM,
        string scenario,
        int warningCount,
        int manualReviewCount)
    {
        var scenarioTitle = scenario switch
        {
            "dense_balanced" => "плотный смешанный",
            "maximum_trees" => "максимум деревьев",
            "maximum_plants" => "максимум количества растений",
            "grass_only" => "травянистое покрытие",
            _ => "сбалансированный"
        };
        _quickSummary.Text =
            $"Готово ({scenarioTitle}): деревьев — {treeCount}, кустарников — {shrubCount}, " +
            $"травянистого покрытия — {areaSquareM:F0} м². " +
            $"Предупреждений — {warningCount}, нерешённых правил — {manualReviewCount}.";
        SetStatus(warningCount == 0
            ? "План озеленения построен и показан на чертеже."
            : "План построен; проверьте предупреждения в журнале.");
        AddLog(
            $"План построен: деревьев {treeCount}, кустарников {shrubCount}, " +
            $"покрытия {areaSquareM:F1} м²; нерешённых правил {manualReviewCount}.",
            warningCount > 0);
    }

    public void ShowPassport(string title, string text, bool error)
    {
        _passport.Text = title + Environment.NewLine + new string('─', Math.Min(title.Length, 40)) +
                         Environment.NewLine + text;
        _passport.BackColor = error ? Color.MistyRose : Color.Honeydew;
        AddLog(title, error);
    }

    public void AddLog(string message, bool error)
    {
        var prefix = error ? "ОШИБКА" : DateTime.Now.ToString("HH:mm:ss");
        _log.Items.Insert(0, $"[{prefix}] {message}");
        if (_log.Items.Count > 200)
            _log.Items.RemoveAt(_log.Items.Count - 1);
    }

    public void SetStatus(string text) => _status.Text = text;

    public void ShowDetails(string title, string text, bool error)
    {
        AddLog(title + ": " + text.Replace(Environment.NewLine, " | "), error);
        ShowPassport(title, text, error);
    }

    private sealed record PlantChoice(
        string Code,
        string Title,
        decimal DefaultSpacing,
        decimal MinimumSpacing,
        bool IsArea = false,
        bool IsMixed = false)
    {
        public override string ToString() => Title;
    }

    private sealed record PatternChoice(string Code, string Title)
    {
        public override string ToString() => Title;
    }

    private sealed record ScenarioChoice(string Code, string Title)
    {
        public override string ToString() => Title;
    }
}
