using System.Text.Json;

namespace GreenAI.Core;

public static class PluginConfigLoader
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
        WriteIndented = true
    };

    public static PluginConfig Load(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException("Plugin configuration was not found", path);
        var config = JsonSerializer.Deserialize<PluginConfig>(File.ReadAllText(path), Options)
            ?? throw new InvalidDataException("Plugin configuration is empty");
        Validate(config);
        return config;
    }

    public static void Validate(PluginConfig config)
    {
        RequireText(config.ConstraintMapFile, "constraintMapFile");
        RequireText(config.NormalizedObjectsFile, "normalizedObjectsFile");
        RequireText(config.ReconstructedUtilitiesFile, "reconstructedUtilitiesFile");
        RequireText(config.BaseAreaObjectType, "baseAreaObjectType");
        RequireText(config.ReportFile, "reportFile");
        if (!double.IsFinite(config.DxfUnitsPerMeter) || config.DxfUnitsPerMeter <= 0)
            throw new InvalidDataException("dxfUnitsPerMeter must be positive");
        if (config.MaxPlacementsPerType <= 0)
            throw new InvalidDataException("maxPlacementsPerType must be positive");
        if (!double.IsFinite(config.ExistingTreeCanopyRadiusM) || config.ExistingTreeCanopyRadiusM < 0)
            throw new InvalidDataException("existingTreeCanopyRadiusM cannot be negative");
        if (config.ExistingTreeClearanceM is double existingTreeClearance &&
            (!double.IsFinite(existingTreeClearance) || existingTreeClearance < 0))
            throw new InvalidDataException("existingTreeClearanceM cannot be negative");
        if (config.PlantingProfiles.Count == 0)
            throw new InvalidDataException("At least one planting profile is required");
        var emptyExclusion = config.HardExclusionObjectTypes.FirstOrDefault(string.IsNullOrWhiteSpace);
        if (emptyExclusion is not null)
            throw new InvalidDataException("hardExclusionObjectTypes cannot contain empty values");
        var duplicateExclusions = config.HardExclusionObjectTypes
            .GroupBy(item => item, StringComparer.OrdinalIgnoreCase)
            .Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        if (duplicateExclusions.Length > 0)
            throw new InvalidDataException("Duplicate hard exclusions: " + string.Join(", ", duplicateExclusions));
        var duplicates = config.PlantingProfiles.GroupBy(item => item.PlantType, StringComparer.OrdinalIgnoreCase)
            .Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        if (duplicates.Length > 0)
            throw new InvalidDataException("Duplicate planting profiles: " + string.Join(", ", duplicates));
        var duplicateLayers = config.PlantingProfiles.GroupBy(item => item.Layer, StringComparer.OrdinalIgnoreCase)
            .Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        if (duplicateLayers.Length > 0)
            throw new InvalidDataException("Planting profiles must use distinct layers: " +
                                           string.Join(", ", duplicateLayers));
        foreach (var profile in config.PlantingProfiles)
        {
            RequireText(profile.PlantType, "plantType");
            RequireText(profile.DisplayName, $"displayName for {profile.PlantType}");
            RequireText(profile.Species, $"species for {profile.PlantType}");
            RequireText(profile.Layer, $"layer for {profile.PlantType}");
            RequireText(profile.CatalogReference, $"catalogReference for {profile.PlantType}");
            if (profile.GeometryKind is not ("point" or "area"))
                throw new InvalidDataException($"Unsupported geometryKind in planting profile {profile.PlantType}");
            if (!double.IsFinite(profile.EdgeClearanceM) ||
                !double.IsFinite(profile.SymbolRadiusM) ||
                !double.IsFinite(profile.FootprintRadiusM) ||
                !double.IsFinite(profile.SpacingM) ||
                !double.IsFinite(profile.AvoidOtherPlantingsM) ||
                profile.EdgeClearanceM < 0 || profile.SymbolRadiusM <= 0 ||
                profile.FootprintRadiusM < 0 || profile.AvoidOtherPlantingsM < 0)
                throw new InvalidDataException($"Invalid distances in planting profile {profile.PlantType}");
            if (profile.GeometryKind == "point" && profile.SpacingM <= 0)
                throw new InvalidDataException($"spacingM must be positive for {profile.PlantType}");
            if (profile.GeometryKind == "point" &&
                profile.SpacingM + 1e-9 < 2.0 * profile.FootprintRadiusM)
                throw new InvalidDataException(
                    $"spacingM cannot be smaller than the mature crown diameter for {profile.PlantType}");
            if (profile.MaxCount.HasValue && profile.MaxCount <= 0)
                throw new InvalidDataException($"maxCount must be positive for {profile.PlantType}");
            if (profile.ColorIndex is < 1 or > 255)
                throw new InvalidDataException($"colorIndex must be between 1 and 255 for {profile.PlantType}");
        }
        var profileTypes = config.PlantingProfiles.Select(item => item.PlantType)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var duplicateRuleCodes = config.Rules.GroupBy(item => item.Code, StringComparer.OrdinalIgnoreCase)
            .Where(group => group.Count() > 1).Select(group => group.Key).ToArray();
        if (duplicateRuleCodes.Length > 0)
            throw new InvalidDataException("Duplicate rule codes: " + string.Join(", ", duplicateRuleCodes));
        foreach (var rule in config.Rules)
        {
            RequireText(rule.Code, "rule code");
            RequireText(rule.PlantType, $"plantType for rule {rule.Code}");
            RequireText(rule.TargetObjectType, $"targetObjectType for rule {rule.Code}");
            RequireText(rule.NormReference, $"normReference for rule {rule.Code}");
            if (!profileTypes.Contains(rule.PlantType))
                throw new InvalidDataException(
                    $"Rule {rule.Code}: unknown planting profile {rule.PlantType}");
            if (rule.Check == "min_distance" &&
                (!rule.MinDistanceM.HasValue || !double.IsFinite(rule.MinDistanceM.Value) ||
                 rule.MinDistanceM < 0))
                throw new InvalidDataException($"Rule {rule.Code}: minDistanceM is required");
            if (rule.Check is not ("min_distance" or "manual_review"))
                throw new InvalidDataException($"Rule {rule.Code}: unsupported check {rule.Check}");
            if (rule.Check == "manual_review")
                RequireText(rule.Reason, $"reason for manual-review rule {rule.Code}");
        }
    }

    private static void RequireText(string? value, string name)
    {
        if (string.IsNullOrWhiteSpace(value))
            throw new InvalidDataException($"{name} cannot be empty");
    }

    public static string ResolveDataDirectory(PluginConfig config, string configPath, string? drawingPath)
    {
        var environment = Environment.GetEnvironmentVariable("GREENAI_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(environment))
            return Path.GetFullPath(environment);

        var candidates = new List<string>();
        if (!string.IsNullOrWhiteSpace(drawingPath))
        {
            var drawingDirectory = Path.GetDirectoryName(Path.GetFullPath(drawingPath));
            if (drawingDirectory is not null)
            {
                candidates.Add(Path.Combine(drawingDirectory, config.DataDirectory));
                candidates.Add(drawingDirectory);
            }
        }
        var configDirectory = Path.GetDirectoryName(Path.GetFullPath(configPath))!;
        candidates.Add(Path.Combine(configDirectory, config.DataDirectory));
        // Packaged plugin folders (build, build-editor, build-friendly) live
        // inside <project>/nanocad-plugin.  Two parent traversals point to the
        // project root where the pipeline places its GeoJSONL artifacts.
        candidates.Add(Path.Combine(configDirectory, "..", "..", "output"));

        foreach (var candidate in candidates.Select(Path.GetFullPath).Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (File.Exists(Path.Combine(candidate, config.ConstraintMapFile)))
                return candidate;
        }
        throw new DirectoryNotFoundException(
            "Could not locate GreenAI data. Set GREENAI_DATA_DIR or edit dataDirectory in greenai.plugin.json.");
    }
}
