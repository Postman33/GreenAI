using System.Text.Json;

namespace GreenAI.Core;

public static class ReportWriter
{
    private static readonly string[] UtilityObjectTypes =
    {
        "water_pipe", "storm_drain", "gas_pipe", "heat_pipe", "sewer_pipe",
        "power_cable", "telecom_cable", "overhead_power_line"
    };

    private static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    };

    public static void WriteRun(PlacementRunResult run, string path)
    {
        var report = new
        {
            generatedAt = run.GeneratedAt,
            scenario = run.Scenario,
            dataDirectory = run.DataDirectory,
            dxfUnitsPerMeter = run.Model.Config.DxfUnitsPerMeter,
            warnings = run.Model.Warnings,
            plantTypes = run.Model.PlantTypes.ToDictionary(
                item => item.Key,
                item => new
                {
                    species = item.Value.Profile.Species,
                    displayName = item.Value.Profile.DisplayName,
                    geometryKind = item.Value.Profile.GeometryKind,
                    spacingM = item.Value.Profile.SpacingM,
                    edgeClearanceM = item.Value.Profile.EdgeClearanceM,
                    footprintRadiusM = item.Value.Profile.FootprintRadiusM,
                    allowedAreaInDxfSquareUnits = item.Value.AllowedArea.Area,
                    verificationScope = "configured_rules_only",
                    utilityTypesWithoutActiveRules = UtilityObjectTypes.Except(
                        run.Model.Config.Rules
                            .Where(rule => rule.PlantType.Equals(item.Key,
                                StringComparison.OrdinalIgnoreCase))
                            .Select(rule => rule.TargetObjectType),
                        StringComparer.OrdinalIgnoreCase).ToArray(),
                    reviewReasons = item.Value.ReviewReasons,
                    rules = item.Value.RuleEvaluations.Select(rule => new
                    {
                        rule.Rule.Code,
                        rule.Rule.TargetObjectType,
                        rule.Rule.Check,
                        rule.Rule.MinDistanceM,
                        rule.Rule.NormReference,
                        rule.Status,
                        rule.GeometrySource
                    })
                }),
            placements = run.Placements.Select(SerializePlacement),
            coverageAreas = run.CoverageAreas.Select(item => new
            {
                item.Id,
                item.PlantType,
                item.Species,
                item.Status,
                areaInDxfSquareUnits = FiniteOrNull(item.Geometry.Area),
                geometryWkt = item.Geometry.AsText(),
                item.AppliedRules,
                item.ReviewReasons,
                checks = item.Checks.Select(SerializeCheck)
            })
        };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        File.WriteAllText(path, JsonSerializer.Serialize(report, Options));
    }

    public static void WriteValidation(
        IReadOnlyList<PlacementValidation> validations,
        string path)
    {
        var report = new
        {
            generatedAt = DateTimeOffset.Now,
            total = validations.Count,
            valid = validations.Count(item => item.IsValid),
            invalid = validations.Count(item => !item.IsValid),
            manualReview = validations.Count(item => item.ReviewReasons.Count > 0),
            // Never pass an NTS Geometry object directly to System.Text.Json.
            // Empty internal envelopes legitimately use +/-Infinity, which is
            // not valid JSON and used to make GREENAI_CHECK fail for objects
            // linked to a selected CAD zone.
            placements = validations.Select(item => new
            {
                placement = new
                {
                    item.Placement.PlantType,
                    x = FiniteOrNull(item.Placement.X),
                    y = FiniteOrNull(item.Placement.Y),
                    item.Placement.SourceId,
                    item.Placement.RequiredAreaSource,
                    requiredAreaWkt = item.Placement.RequiredArea?.AsText()
                },
                item.IsValid,
                item.Errors,
                item.ReviewReasons,
                checks = item.Checks?.Select(SerializeCheck)
            })
        };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        File.WriteAllText(path, JsonSerializer.Serialize(report, Options));
    }

    private static object SerializePlacement(PlacementPoint item) => new
    {
        item.PlantType,
        item.Species,
        x = FiniteOrNull(item.X),
        y = FiniteOrNull(item.Y),
        item.Status,
        item.AppliedRules,
        item.ReviewReasons,
        item.Id,
        footprintRadiusM = FiniteOrNull(item.FootprintRadiusM),
        checks = item.Checks?.Select(SerializeCheck)
    };

    private static object SerializeCheck(RuleCheckResult item) => new
    {
        item.Code,
        item.Target,
        item.Status,
        actualDistanceM = FiniteOrNull(item.ActualDistanceM),
        requiredDistanceM = FiniteOrNull(item.RequiredDistanceM),
        item.NormReference,
        item.Explanation
    };

    private static double? FiniteOrNull(double? value) =>
        value.HasValue && double.IsFinite(value.Value) ? value.Value : null;
}
