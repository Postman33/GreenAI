using System.Drawing;
using System.Windows.Forms;

namespace GreenAI.NanoCad;

internal sealed class GreenAiPlantingDialog : Form
{
    private readonly ComboBox _plant = new();
    private readonly NumericUpDown _spacing = new();
    private readonly NumericUpDown _maxCount = new();

    public GreenAiPlantingDialog(bool allowAreaPlans)
    {
        Text = "GreenAI — параметры посадки";
        Font = new Font("Segoe UI", 9F);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(390, 218);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12),
            ColumnCount = 2,
            RowCount = 5
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 40F));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 60F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 36F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 30F));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42F));

        _plant.DropDownStyle = ComboBoxStyle.DropDownList;
        _plant.Items.Add(new PlantOption(
            "tree", "Деревья — липа; расчётная крона Ø 5 м", 5M, 5M, false));
        _plant.Items.Add(new PlantOption(
            "shrub", "Кустарники — спирея; расчётная крона Ø 1,5 м", 2M, 1.5M, false));
        if (allowAreaPlans)
        {
            _plant.Items.Add(new PlantOption("mixed", "Смешанный план", 5M, 0.5M, false));
            _plant.Items.Add(new PlantOption(
                "herbaceous", "Газон / травянистое покрытие", 0.5M, 0.5M, true));
        }
        _plant.SelectedIndex = 0;
        _plant.SelectedIndexChanged += (_, _) => ApplyPlantDefaults();

        _spacing.DecimalPlaces = 1;
        _spacing.Minimum = 0.5M;
        _spacing.Maximum = 100M;
        _spacing.Increment = 0.5M;
        _spacing.Value = 5M;
        _maxCount.Minimum = 1;
        _maxCount.Maximum = 5000;
        _maxCount.Value = 5000;
        ApplyPlantDefaults();

        AddField(layout, 0, "Что посадить", _plant);
        AddField(layout, 1, "Шаг, м", _spacing);
        AddField(layout, 2, "Максимум", _maxCount);
        var hint = new Label
        {
            Text = allowAreaPlans
                ? "Сначала будет построен голубой предпросмотр внутри выбранного контура."
                : "Сначала будет построен голубой предпросмотр вдоль выбранной линии.",
            Dock = DockStyle.Fill,
            AutoSize = false,
            ForeColor = SystemColors.GrayText
        };
        layout.SetColumnSpan(hint, 2);
        layout.Controls.Add(hint, 0, 3);

        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            WrapContents = false
        };
        var preview = new Button
        {
            Text = "Построить предпросмотр",
            AutoSize = true,
            DialogResult = DialogResult.OK
        };
        var cancel = new Button
        {
            Text = "Отмена",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        buttons.Controls.Add(preview);
        buttons.Controls.Add(cancel);
        layout.SetColumnSpan(buttons, 2);
        layout.Controls.Add(buttons, 0, 4);
        Controls.Add(layout);
        AcceptButton = preview;
        CancelButton = cancel;
    }

    public GreenAiContextRequest Request
    {
        get
        {
            var option = (PlantOption)_plant.SelectedItem!;
            return new GreenAiContextRequest(option.Code, (double)_spacing.Value, (int)_maxCount.Value);
        }
    }

    private void ApplyPlantDefaults()
    {
        if (_plant.SelectedItem is not PlantOption option)
            return;
        _spacing.Minimum = option.MinimumSpacing;
        _spacing.Value = option.DefaultSpacing;
        _spacing.Enabled = !option.IsArea;
        _maxCount.Enabled = !option.IsArea;
    }

    private static void AddField(TableLayoutPanel layout, int row, string title, Control control)
    {
        layout.Controls.Add(new Label
        {
            Text = title,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleLeft
        }, 0, row);
        control.Dock = DockStyle.Fill;
        layout.Controls.Add(control, 1, row);
    }

    private sealed record PlantOption(
        string Code,
        string Title,
        decimal DefaultSpacing,
        decimal MinimumSpacing,
        bool IsArea)
    {
        public override string ToString() => Title;
    }
}
