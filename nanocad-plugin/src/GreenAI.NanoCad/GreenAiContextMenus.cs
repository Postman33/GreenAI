using HostMgd.Windows;
using System.Text.Json;
using Teigha.DatabaseServices;
using Teigha.Runtime;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

internal static class GreenAiContextMenus
{
    private static ContextMenuExtension? _defaultMenu;
    private static ContextMenuExtension? _entityMenu;

    public static bool IsRegistered => _defaultMenu is not null && _entityMenu is not null;

    public static void Register()
    {
        if (_defaultMenu is not null)
            return;

        _defaultMenu = CreateDefaultMenu();
        _entityMenu = CreateEntityMenu();
        HostApplication.AddDefaultContextMenuExtension(_defaultMenu);
        HostApplication.AddObjectContextMenuExtension(
            RXObject.GetClass(typeof(Entity)), _entityMenu);
    }

    public static void Unregister()
    {
        if (_defaultMenu is not null)
            HostApplication.RemoveDefaultContextMenuExtension(_defaultMenu);
        if (_entityMenu is not null)
            HostApplication.RemoveObjectContextMenuExtension(
                RXObject.GetClass(typeof(Entity)), _entityMenu);
        _defaultMenu = null;
        _entityMenu = null;
    }

    private static ContextMenuExtension CreateDefaultMenu()
    {
        var extension = new ContextMenuExtension { Title = "GreenAI" };
        foreach (var item in CreatePlantingItems(includeAreaPlans: true))
            extension.MenuItems.Add(item);
        return extension;
    }

    private static ContextMenuExtension CreateEntityMenu()
    {
        var extension = new ContextMenuExtension();
        var greenAi = new HostMgd.Windows.MenuItem("GreenAI");
        foreach (var item in CreatePlantingItems(includeAreaPlans: true))
            greenAi.MenuItems.Add(item);
        extension.MenuItems.Add(greenAi);
        return extension;
    }

    private static IEnumerable<HostMgd.Windows.MenuItem> CreatePlantingItems(
        bool includeAreaPlans)
    {
        var tree = new HostMgd.Windows.MenuItem("Деревья — шаг 5 м");
        tree.Click += (_, _) => StartPreset("tree", 5.0);
        yield return tree;
        var shrub = new HostMgd.Windows.MenuItem("Кустарники — шаг 2 м");
        shrub.Click += (_, _) => StartPreset("shrub", 2.0);
        yield return shrub;
        if (includeAreaPlans)
        {
            var mixed = new HostMgd.Windows.MenuItem("Деревья + кустарники + газон");
            mixed.Click += (_, _) => StartPreset("mixed", 5.0);
            yield return mixed;
            var grass = new HostMgd.Windows.MenuItem("Газон / травянистое покрытие");
            grass.Click += (_, _) => StartPreset("herbaceous", 0.5);
            yield return grass;
        }
        var custom = new HostMgd.Windows.MenuItem("Параметры посадки…");
        custom.Click += (_, _) => StartCustom();
        yield return custom;
        var apply = new HostMgd.Windows.MenuItem("Добавить текущий предпросмотр");
        apply.Click += (_, _) => SendCommand("GREENAI_APPLY");
        yield return apply;
        var zoom = new HostMgd.Windows.MenuItem("Показать последний результат");
        zoom.Click += (_, _) => SendCommand("GREENAI_ZOOM_RESULT");
        yield return zoom;
        var panel = new HostMgd.Windows.MenuItem("Открыть расширенную панель");
        panel.Click += (_, _) => SendCommand("GREENAI_PANEL");
        yield return panel;
    }

    private static void StartPreset(string plantType, double spacingM)
    {
        WriteInteractionProbe("preset", plantType, spacingM);
        GreenAiContextRequest.Pending = new GreenAiContextRequest(plantType, spacingM, 5000);
        SendCommand("GREENAI_CONTEXT_PREVIEW");
    }

    private static void StartCustom()
    {
        WriteInteractionProbe("custom", null, null);
        GreenAiContextRequest.Pending = null;
        SendCommand("GREENAI_CONTEXT_CUSTOM");
    }

    private static void WriteInteractionProbe(string action, string? plantType, double? spacingM)
    {
        var path = Environment.GetEnvironmentVariable("GREENAI_CONTEXT_PROBE_FILE");
        if (string.IsNullOrWhiteSpace(path))
            return;
        var directory = Path.GetDirectoryName(Path.GetFullPath(path));
        if (!string.IsNullOrWhiteSpace(directory))
            Directory.CreateDirectory(directory);
        File.WriteAllText(path, JsonSerializer.Serialize(new
        {
            action,
            plantType,
            spacingM,
            invokedAt = DateTimeOffset.Now,
            processId = Environment.ProcessId
        }, new JsonSerializerOptions { WriteIndented = true }));
    }

    private static void SendCommand(string command)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        document?.SendStringToExecute(command + " ", true, false, false);
    }
}

internal sealed record GreenAiContextRequest(string PlantType, double SpacingM, int MaxCount)
{
    public static GreenAiContextRequest? Pending { get; set; }
}
