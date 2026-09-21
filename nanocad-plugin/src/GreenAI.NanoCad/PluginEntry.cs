using System.Reflection;
using System.Text.Json;
using HostMgd.ApplicationServices;
using Teigha.Runtime;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

public sealed class PluginEntry : IExtensionApplication
{
    private static bool _autorunAttached;
    private static bool _warmupAttached;

    public void Initialize()
    {
        GreenAiAutoValidator.Initialize();
        GreenAiContextMenus.Register();
        HostApplication.Idle += OnWarmupIdle;
        _warmupAttached = true;

        // Used only by the automated load probe. Normal interactive loading
        // does not create any files or run calculations by itself.
        var probePath = Environment.GetEnvironmentVariable("GREENAI_PLUGIN_PROBE_FILE");
        if (string.IsNullOrWhiteSpace(probePath))
            return;
        var payload = new
        {
            status = "loaded",
            loadedAt = DateTimeOffset.Now,
            assembly = Assembly.GetExecutingAssembly().Location,
            runtime = Environment.Version.ToString(),
            processId = Environment.ProcessId
        };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(probePath))!);
        File.WriteAllText(probePath, JsonSerializer.Serialize(payload, new JsonSerializerOptions
        {
            WriteIndented = true
        }));

        if (Environment.GetEnvironmentVariable("GREENAI_PLUGIN_AUTORUN") == "1")
        {
            HostApplication.Idle += OnIdle;
            _autorunAttached = true;
        }
    }

    public void Terminate()
    {
        if (_autorunAttached)
        {
            HostApplication.Idle -= OnIdle;
            _autorunAttached = false;
        }
        if (_warmupAttached)
        {
            HostApplication.Idle -= OnWarmupIdle;
            _warmupAttached = false;
        }
        GreenAiAutoValidator.Terminate();
        GreenAiContextMenus.Unregister();
        GreenAiPanel.Close();
    }

    private static void OnIdle(object? sender, EventArgs args)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;
        HostApplication.Idle -= OnIdle;
        _autorunAttached = false;
        document.SendStringToExecute("GREENAI_AUTORUN_SMOKE ", true, false, false);
    }

    private static void OnWarmupIdle(object? sender, EventArgs args)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;
        HostApplication.Idle -= OnWarmupIdle;
        _warmupAttached = false;
        if (Environment.GetEnvironmentVariable("GREENAI_CONTEXT_VISUAL_PREP") == "1")
            document.SendStringToExecute("GREENAI_CONTEXT_VISUAL_PREP ", true, false, false);
        var drawingPath = document.Name;
        _ = Task.Run(() =>
        {
            try
            {
                Commands.WarmupForDrawing(drawingPath);
            }
            catch
            {
                // The regular command reports configuration/data errors with
                // full UI context. Warm-up must never interrupt nanoCAD startup.
            }
        });
    }
}
