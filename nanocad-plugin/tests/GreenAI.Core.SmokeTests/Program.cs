using GreenAI.Core;
using System.Diagnostics;

if (args.Length != 2)
{
    Console.Error.WriteLine("Usage: GreenAI.Core.SmokeTests <config.json> <data-directory>");
    return 2;
}

var config = PluginConfigLoader.Load(args[0]);
var invalidDistanceConfig = CloneConfig(config);
invalidDistanceConfig.PlantingProfiles[0].SpacingM = double.NaN;
ExpectThrows<InvalidDataException>(() => PluginConfigLoader.Validate(invalidDistanceConfig),
    "Config accepted a non-finite planting distance");
var invalidReferenceConfig = CloneConfig(config);
invalidReferenceConfig.Rules[0].PlantType = "missing-plant-profile";
ExpectThrows<InvalidDataException>(() => PluginConfigLoader.Validate(invalidReferenceConfig),
    "Config accepted a rule referencing an unknown plant profile");
var duplicateLayerConfig = CloneConfig(config);
duplicateLayerConfig.PlantingProfiles[1].Layer = duplicateLayerConfig.PlantingProfiles[0].Layer;
ExpectThrows<InvalidDataException>(() => PluginConfigLoader.Validate(duplicateLayerConfig),
    "Config accepted duplicate result layers");
var engine = new PlacementEngine();
var stopwatch = Stopwatch.StartNew();
var run = engine.Run(config, Path.GetFullPath(args[1]));
var runElapsed = stopwatch.Elapsed;
if (run.Placements.Count == 0)
    throw new InvalidOperationException("No placements generated");
if (run.CoverageAreas.Count == 0)
    throw new InvalidOperationException("No herbaceous coverage generated");
if (config.ProtectExistingVegetation &&
    (!run.Model.HardExclusions.ContainsKey("existing_tree") ||
     !run.Model.HardExclusions.ContainsKey("existing_tree_belt")))
    throw new InvalidOperationException("Existing vegetation is not protected");
foreach (var area in run.CoverageAreas)
{
    if (area.Geometry.Difference(run.Model.BaseArea).Area > 1e-6)
        throw new InvalidOperationException($"Coverage {area.Id} protrudes outside base area");
    // An existing tree blocks new point plantings, but lawn/ground cover may continue
    // beneath its canopy. All other physical exclusions still have to stay empty.
    if (run.Model.HardExclusions.Any(item =>
            !item.Key.Equals("existing_tree", StringComparison.OrdinalIgnoreCase) &&
            item.Value.Intersection(area.Geometry).Area > 1e-6))
        throw new InvalidOperationException($"Coverage {area.Id} overlaps a physical exclusion");
}
var shrubCoverage = run.CoverageAreas
    .Where(item => item.PlantType.Equals("shrub", StringComparison.OrdinalIgnoreCase))
    .Select(item => item.Geometry)
    .ToArray();
if (shrubCoverage.Length == 0)
    throw new InvalidOperationException("No mass shrub coverage generated");
var herbaceousCoverage = run.CoverageAreas
    .Where(item => item.PlantType.Equals("herbaceous", StringComparison.OrdinalIgnoreCase))
    .Select(item => item.Geometry)
    .ToArray();
if (shrubCoverage.Any(shrub => herbaceousCoverage.Any(
        herbaceous => shrub.Intersection(herbaceous).Area > 1e-6)))
    throw new InvalidOperationException("Shrub beds overlap herbaceous coverage");
if (run.Placements.Where(item => item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase))
    .Any(tree => shrubCoverage.Any(shrub => shrub.Intersects(
        shrub.Factory.CreatePoint(new NetTopologySuite.Geometries.Coordinate(tree.X, tree.Y))
            .Buffer((tree.FootprintRadiusM + 0.2) * config.DxfUnitsPerMeter)))))
    throw new InvalidOperationException("Balanced shrub beds overlap tree footprints");
if (run.Placements.Select(item => item.Id).Distinct(StringComparer.OrdinalIgnoreCase).Count() !=
    run.Placements.Count)
    throw new InvalidOperationException("Generated placement ids are not unique");
if (run.Placements.Any(item =>
        string.IsNullOrWhiteSpace(item.Id) ||
        item.Checks is null || item.Checks.Count == 0 ||
        item.Checks.All(check => check.Code != "PLANT_SELECTION") ||
        item.Checks.All(check => string.IsNullOrWhiteSpace(check.NormReference))))
    throw new InvalidOperationException("A generated placement has no stable id, selection reason or normative checks");
var reportPath = Path.Combine(Path.GetTempPath(), $"greenai-smoke-{Guid.NewGuid():N}.json");
try
{
    ReportWriter.WriteRun(run, reportPath);
    using var reportJson = System.Text.Json.JsonDocument.Parse(File.ReadAllText(reportPath));
    if (!reportJson.RootElement.TryGetProperty("placements", out var reportPlacements) ||
        reportPlacements.GetArrayLength() != run.Placements.Count)
        throw new InvalidOperationException("Per-placement report is incomplete");
    var treeReport = reportJson.RootElement.GetProperty("plantTypes").GetProperty("tree");
    if (treeReport.GetProperty("verificationScope").GetString() != "configured_rules_only" ||
        !treeReport.GetProperty("utilityTypesWithoutActiveRules")
            .EnumerateArray().Any(item => item.GetString() == "sewer_pipe"))
        throw new InvalidOperationException("Unchecked utility types are missing from the report");
}
finally
{
    if (File.Exists(reportPath))
        File.Delete(reportPath);
}

var existing = run.Placements.Select((item, index) =>
    new ExistingPlacement(item.PlantType, item.X, item.Y, index.ToString())).ToArray();
var validations = engine.Validate(run.Model, existing);
if (validations.Any(item => !item.IsValid))
{
    var firstInvalid = validations.First(item => !item.IsValid);
    throw new InvalidOperationException(
        $"Generated placements failed validation: {validations.Count(item => !item.IsValid)} invalid; " +
        $"{firstInvalid.Placement.SourceId}: {string.Join(" | ", firstInvalid.Errors)}; " +
        $"core run {runElapsed.TotalSeconds:F2}s");
}

var maximumTrees = engine.Generate(run.Model, "maximum_trees");
if (maximumTrees.Count == 0 || maximumTrees.Any(item => item.PlantType != "tree"))
    throw new InvalidOperationException("Maximum-trees scenario returned an invalid plant mix");
var maximumPlants = engine.Generate(run.Model, "maximum_plants");
if (maximumPlants.Count < run.Placements.Count ||
    maximumPlants.Select(item => item.PlantType).Distinct(StringComparer.OrdinalIgnoreCase).Count() != 1)
    throw new InvalidOperationException(
        $"Maximum-plants scenario did not maximize the point count: balanced={run.Placements.Count}, maximum={maximumPlants.Count}");
var denseBalanced = engine.Generate(run.Model, "dense_balanced");
if (denseBalanced.Count <= run.Placements.Count)
    throw new InvalidOperationException("Dense-balanced scenario did not add more point plantings");
var grassOnly = engine.Generate(run.Model, "grass_only");
if (grassOnly.Count != 0)
    throw new InvalidOperationException("Grass-only scenario generated point plantings");

var treeModel = run.Model.PlantTypes["tree"];
var selectedZone = treeModel.AllowedArea.Envelope;
var selectedPlacements = engine.GenerateInArea(
    run.Model, "tree", selectedZone, "grid", 6.0, 20, Array.Empty<ExistingPlacement>());
if (selectedPlacements.Count == 0 || selectedPlacements.Count > 20)
    throw new InvalidOperationException("GenerateInArea did not respect the selected zone or limit");
foreach (var placement in selectedPlacements)
{
    var footprint = treeModel.AllowedArea.Factory
        .CreatePoint(new NetTopologySuite.Geometries.Coordinate(placement.X, placement.Y))
        .Buffer(placement.FootprintRadiusM * config.DxfUnitsPerMeter);
    if (!selectedZone.Covers(footprint))
        throw new InvalidOperationException("A mature plant footprint protrudes outside selected zone");
}
var selectedValidation = engine.Validate(
    run.Model,
    selectedPlacements.Select((item, index) =>
        new ExistingPlacement(item.PlantType, item.X, item.Y, $"selected-{index}")).ToArray());
if (selectedValidation.Any(item => !item.IsValid))
    throw new InvalidOperationException("Selected-zone placements failed validation");

var guideAnchor = selectedPlacements[0];
var guide = treeModel.AllowedArea.Factory.CreateLineString(new[]
{
    new NetTopologySuite.Geometries.Coordinate(guideAnchor.X - 0.5, guideAnchor.Y),
    new NetTopologySuite.Geometries.Coordinate(guideAnchor.X + 0.5, guideAnchor.Y)
});
var guidePlacements = engine.GenerateAlongGuide(
    run.Model, "tree", guide, 6.0, 20, Array.Empty<ExistingPlacement>());
if (guidePlacements.Count != 1 ||
    Math.Abs(guidePlacements[0].X - guideAnchor.X) > 1e-7 ||
    Math.Abs(guidePlacements[0].Y - guideAnchor.Y) > 1e-7)
    throw new InvalidOperationException("Guide-line placement was not centred on the selected line");

ExpectThrows<ArgumentException>(() =>
    engine.GenerateAlongGuide(run.Model, "tree", selectedZone, 6.0, 20),
    "A polygon was accepted as a guide line");
ExpectThrows<ArgumentException>(() =>
    engine.GenerateInArea(run.Model, "tree", guide, "grid", 6.0, 20),
    "A line was accepted as a planting area");

var nonFiniteValidation = engine.Validate(run.Model, new[]
{
    new ExistingPlacement("tree", double.PositiveInfinity, guideAnchor.Y, "non-finite")
})[0];
if (nonFiniteValidation.IsValid ||
    (nonFiniteValidation.Checks ?? Array.Empty<RuleCheckResult>()).All(item =>
        item.Code != "FINITE_COORDINATES" || item.Status != "failed"))
    throw new InvalidOperationException("Non-finite placement coordinates were not rejected cleanly");

var curvedCenter = treeModel.AllowedArea.PointOnSurface;
var curvedZone = curvedCenter.Buffer(20.0 * config.DxfUnitsPerMeter, 32);
var curvedPlacements = engine.GenerateInArea(
    run.Model, "tree", curvedZone, "grid", 6.0, 100, Array.Empty<ExistingPlacement>());
if (curvedPlacements.Count == 0)
    throw new InvalidOperationException("Curved-zone generation returned no placements");
foreach (var placement in curvedPlacements)
{
    var footprint = curvedZone.Factory
        .CreatePoint(new NetTopologySuite.Geometries.Coordinate(placement.X, placement.Y))
        .Buffer(placement.FootprintRadiusM * config.DxfUnitsPerMeter);
    if (!curvedZone.Covers(footprint))
        throw new InvalidOperationException("A planting protrudes outside a curved selected zone");
}

var editable = curvedPlacements[0];
var editablePoint = curvedZone.Factory.CreatePoint(
    new NetTopologySuite.Geometries.Coordinate(editable.X, editable.Y));
var editZone = editablePoint.Buffer((editable.FootprintRadiusM + 1.0) * config.DxfUnitsPerMeter, 32);
var insideEdit = engine.Validate(run.Model, new[]
{
    new ExistingPlacement(
        editable.PlantType, editable.X, editable.Y, "edit-inside", editZone, "тестовый контур")
})[0];
if (!insideEdit.IsValid || (insideEdit.Checks ?? Array.Empty<RuleCheckResult>()).All(item =>
        item.Code != "WITHIN_SELECTED_ZONE" || item.Status != "passed"))
    throw new InvalidOperationException("A point inside its editable zone did not pass the zone check");
var movedOutsideEdit = engine.Validate(run.Model, new[]
{
    new ExistingPlacement(
        editable.PlantType,
        editable.X + (editable.FootprintRadiusM + 2.0) * config.DxfUnitsPerMeter,
        editable.Y,
        "edit-outside",
        editZone,
        "тестовый контур")
})[0];
if (movedOutsideEdit.IsValid || (movedOutsideEdit.Checks ?? Array.Empty<RuleCheckResult>()).All(item =>
        item.Code != "WITHIN_SELECTED_ZONE" || item.Status != "failed"))
    throw new InvalidOperationException("A moved point outside its editable zone was not rejected");

var validationReportPath = Path.Combine(Path.GetTempPath(), $"greenai-validation-{Guid.NewGuid():N}.json");
try
{
    var reportValidation = new PlacementValidation(
        new ExistingPlacement("tree", editable.X, editable.Y, "report-zone", editZone, "test zone"),
        true,
        Array.Empty<string>(),
        Array.Empty<string>(),
        new[]
        {
            new RuleCheckResult(
                "FINITE_JSON", "test", "passed", double.PositiveInfinity, 1.0,
                "test", "Non-finite library distance must be represented as null")
        });
    ReportWriter.WriteValidation(new[] { reportValidation }, validationReportPath);
    using var validationJson = System.Text.Json.JsonDocument.Parse(File.ReadAllText(validationReportPath));
    var serializedDistance = validationJson.RootElement.GetProperty("placements")[0]
        .GetProperty("checks")[0].GetProperty("actualDistanceM");
    if (serializedDistance.ValueKind != System.Text.Json.JsonValueKind.Null)
        throw new InvalidOperationException("Non-finite validation distance was written to JSON");
}
finally
{
    if (File.Exists(validationReportPath))
        File.Delete(validationReportPath);
}

var outside = run.Model.BaseArea.EnvelopeInternal;
var invalid = engine.Validate(run.Model, new[]
{
    new ExistingPlacement("tree", outside.MaxX + 1000, outside.MaxY + 1000, "outside")
})[0];
if (invalid.IsValid || invalid.Errors.All(item =>
        !item.Contains("выходит за подтверждённую область") &&
        !item.Contains("вне подтверждённой области")))
    throw new InvalidOperationException("Invalid manual point has no meaningful rejection reason");

Console.WriteLine($"PASS: {run.Placements.Count} placements");
foreach (var group in run.Placements.GroupBy(item => item.PlantType))
    Console.WriteLine($"  {group.Key}: {group.Count()}");
Console.WriteLine($"Warnings: {run.Model.Warnings.Count}");
Console.WriteLine($"Coverage areas: {run.CoverageAreas.Count}");
Console.WriteLine($"Core run: {runElapsed.TotalSeconds:F2}s");
Console.WriteLine($"Selected-zone placements: {selectedPlacements.Count}");
Console.WriteLine($"Guide-line placements: {guidePlacements.Count}");
Console.WriteLine($"Curved-zone placements: {curvedPlacements.Count}");
Console.WriteLine($"Maximum trees: {maximumTrees.Count}");
Console.WriteLine($"Maximum plants: {maximumPlants.Count}");
foreach (var warning in run.Model.Warnings)
    Console.WriteLine($"  {warning}");
return 0;

static void ExpectThrows<TException>(Action action, string message) where TException : Exception
{
    try
    {
        action();
    }
    catch (TException)
    {
        return;
    }
    throw new InvalidOperationException(message);
}

static PluginConfig CloneConfig(PluginConfig source) =>
    System.Text.Json.JsonSerializer.Deserialize<PluginConfig>(
        System.Text.Json.JsonSerializer.Serialize(source))
    ?? throw new InvalidOperationException("Could not clone plugin configuration for contract tests");
