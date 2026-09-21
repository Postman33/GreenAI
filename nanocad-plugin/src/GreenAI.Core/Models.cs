using System.Text.Json.Serialization;
using NetTopologySuite.Geometries;
using NetTopologySuite.Operation.Distance;

namespace GreenAI.Core;

public sealed class PluginConfig
{
    public string DataDirectory { get; set; } = ".";
    public string ConstraintMapFile { get; set; } = "constraint_map.geojsonl";
    public string NormalizedObjectsFile { get; set; } = "normalized_objects.geojsonl";
    public string ReconstructedUtilitiesFile { get; set; } = "reconstructed_utilities.geojsonl";
    public string BaseAreaObjectType { get; set; } = "confirmed_plantable_surface";
    public List<string> HardExclusionObjectTypes { get; set; } = new();
    public string ReportFile { get; set; } = "nanocad_plugin_report.json";
    public double DxfUnitsPerMeter { get; set; } = 1.0;
    public int MaxPlacementsPerType { get; set; } = 1500;
    public bool ProtectExistingVegetation { get; set; } = true;
    public double ExistingTreeCanopyRadiusM { get; set; } = 2.5;
    public double? ExistingTreeClearanceM { get; set; }
    public List<PlantingProfile> PlantingProfiles { get; set; } = new();
    public List<PlacementRule> Rules { get; set; } = new();
}

public sealed class PlantingProfile
{
    public string PlantType { get; set; } = "tree";
    public string DisplayName { get; set; } = "Посадка";
    public string Species { get; set; } = "Не выбран";
    public string GeometryKind { get; set; } = "point";
    public double SpacingM { get; set; } = 5.0;
    public double EdgeClearanceM { get; set; } = 1.0;
    public double SymbolRadiusM { get; set; } = 1.0;
    public double FootprintRadiusM { get; set; } = 1.0;
    public double AvoidOtherPlantingsM { get; set; } = 0.0;
    public int? MaxCount { get; set; }
    public List<string> SelectionReasons { get; set; } = new();
    public string CatalogReference { get; set; } = "Каталог растений проекта";
    public string Layer { get; set; } = "GREEN_AI_PLANT_TREE";
    public short ColorIndex { get; set; } = 3;
}

public sealed class PlacementRule
{
    public string Code { get; set; } = "";
    public string PlantType { get; set; } = "tree";
    public string TargetObjectType { get; set; } = "";
    public string Check { get; set; } = "min_distance";
    public double? MinDistanceM { get; set; }
    public bool RequireReconstructedGeometry { get; set; }
    public string NormReference { get; set; } = "";
    public string Reason { get; set; } = "";
}

public sealed record PlacementPoint(
    string PlantType,
    string Species,
    double X,
    double Y,
    string Status,
    IReadOnlyList<string> AppliedRules,
    IReadOnlyList<string> ReviewReasons,
    string Id = "",
    double FootprintRadiusM = 0,
    IReadOnlyList<RuleCheckResult>? Checks = null);

public sealed record RuleCheckResult(
    string Code,
    string Target,
    string Status,
    double? ActualDistanceM,
    double? RequiredDistanceM,
    string NormReference,
    string Explanation);

public sealed record PlantingArea(
    string Id,
    string PlantType,
    string Species,
    string Status,
    Geometry Geometry,
    IReadOnlyList<string> AppliedRules,
    IReadOnlyList<string> ReviewReasons,
    IReadOnlyList<RuleCheckResult> Checks);

public sealed record ExistingPlacement(
    string PlantType,
    double X,
    double Y,
    string SourceId,
    Geometry? RequiredArea = null,
    string? RequiredAreaSource = null);

public sealed record PlacementValidation(
    ExistingPlacement Placement,
    bool IsValid,
    IReadOnlyList<string> Errors,
    IReadOnlyList<string> ReviewReasons,
    IReadOnlyList<RuleCheckResult>? Checks = null);

public sealed class PreparedPlantType
{
    public PlantingProfile Profile { get; init; } = null!;
    [JsonIgnore]
    public Geometry AllowedArea { get; init; } = null!;
    public IReadOnlyList<RuleEvaluation> RuleEvaluations { get; init; } = Array.Empty<RuleEvaluation>();
    public IReadOnlyList<string> ReviewReasons { get; init; } = Array.Empty<string>();
}

public sealed class RuleEvaluation
{
    public PlacementRule Rule { get; init; } = null!;
    public string Status { get; init; } = "applied";
    public string GeometrySource { get; init; } = "normalized";
    [JsonIgnore]
    public Geometry? TargetGeometry { get; init; }
    [JsonIgnore]
    public IndexedFacetDistance? DistanceIndex { get; init; }
}

public sealed class PreparedModel
{
    public PluginConfig Config { get; init; } = null!;
    public IReadOnlyDictionary<string, PreparedPlantType> PlantTypes { get; init; } = null!;
    public IReadOnlyDictionary<string, Geometry> NormalizedObjects { get; init; } = null!;
    public IReadOnlyDictionary<string, Geometry> ReconstructedUtilities { get; init; } = null!;
    [JsonIgnore]
    public Geometry BaseArea { get; init; } = null!;
    [JsonIgnore]
    public IReadOnlyDictionary<string, Geometry> HardExclusions { get; init; } =
        new Dictionary<string, Geometry>();
    [JsonIgnore]
    public IReadOnlyDictionary<string, IndexedFacetDistance> HardExclusionDistanceIndexes { get; init; } =
        new Dictionary<string, IndexedFacetDistance>();
    public IReadOnlyList<string> Warnings { get; init; } = Array.Empty<string>();
}

public sealed class PlacementRunResult
{
    public PreparedModel Model { get; init; } = null!;
    public IReadOnlyList<PlacementPoint> Placements { get; init; } = Array.Empty<PlacementPoint>();
    public IReadOnlyList<PlantingArea> CoverageAreas { get; init; } = Array.Empty<PlantingArea>();
    public string DataDirectory { get; init; } = "";
    public DateTimeOffset GeneratedAt { get; init; }
    public string Scenario { get; init; } = "balanced";
}
