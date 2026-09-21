using GreenAI.Core;

namespace GreenAI.NanoCad;

/// <summary>
/// Reuses the expensive parsed and unioned GeoJSON model while its source
/// files are unchanged. Commands are short-lived, so the cache is process-wide.
/// </summary>
internal static class PreparedModelCache
{
    private static readonly object Sync = new();
    private static string _signature = "";
    private static PreparedModel? _model;

    public static PreparedModel Get(
        PlacementEngine engine,
        PluginConfig config,
        string configPath,
        string dataDirectory)
    {
        var signature = BuildSignature(config, configPath, dataDirectory);
        lock (Sync)
        {
            if (_model is not null && signature.Equals(_signature, StringComparison.Ordinal))
                return _model;

            _model = engine.Prepare(config, dataDirectory);
            _signature = signature;
            return _model;
        }
    }

    public static void Clear()
    {
        lock (Sync)
        {
            _model = null;
            _signature = "";
        }
    }

    private static string BuildSignature(
        PluginConfig config,
        string configPath,
        string dataDirectory) =>
        string.Join("|", new[]
        {
            FileStamp(configPath),
            FileStamp(Path.Combine(dataDirectory, config.ConstraintMapFile)),
            FileStamp(Path.Combine(dataDirectory, config.NormalizedObjectsFile)),
            FileStamp(Path.Combine(dataDirectory, config.ReconstructedUtilitiesFile))
        });

    private static string FileStamp(string path)
    {
        var fullPath = Path.GetFullPath(path);
        var file = new FileInfo(fullPath);
        return file.Exists
            ? $"{fullPath}:{file.Length}:{file.LastWriteTimeUtc.Ticks}"
            : $"{fullPath}:missing";
    }
}
