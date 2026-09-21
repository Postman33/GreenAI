using NetTopologySuite.Geometries;
using NetTopologySuite.Geometries.Prepared;
using NetTopologySuite.Operation.Distance;
using NetTopologySuite.Operation.Overlay;
using NetTopologySuite.Operation.OverlayNG;
using NetTopologySuite.LinearReferencing;

namespace GreenAI.Core;

public sealed class PlacementEngine
{
    private static readonly HashSet<string> UtilityTypes = new(StringComparer.OrdinalIgnoreCase)
    {
        "water_pipe", "storm_drain", "gas_pipe", "heat_pipe", "sewer_pipe",
        "power_cable", "telecom_cable", "overhead_power_line"
    };
    private static readonly IReadOnlyDictionary<string, string> ConstraintAliases =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["building"] = "buildings_in_work_area",
            ["sidewalk"] = "sidewalk_area",
            ["road_edge"] = "road_area"
        };

    private readonly GeoJsonlGeometryReader _reader = new();
    private readonly GeometryFactory _geometryFactory = new(new PrecisionModel(), 0);

    public PlacementRunResult Run(
        PluginConfig config,
        string dataDirectory,
        string scenario = "balanced")
    {
        var model = Prepare(config, dataDirectory);
        return Run(model, dataDirectory, scenario);
    }

    public PlacementRunResult Run(
        PreparedModel model,
        string dataDirectory,
        string scenario = "balanced")
    {
        var placements = Generate(model, scenario);
        return new PlacementRunResult
        {
            Model = model,
            Placements = placements,
            CoverageAreas = GenerateCoverageAreas(model, model.BaseArea, placements, scenario),
            DataDirectory = dataDirectory,
            GeneratedAt = DateTimeOffset.Now,
            Scenario = scenario
        };
    }

    public PreparedModel Prepare(PluginConfig config, string dataDirectory)
    {
        var constraintTypes = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            config.BaseAreaObjectType
        };
        foreach (var alias in ConstraintAliases.Values)
            constraintTypes.Add(alias);
        foreach (var exclusion in config.HardExclusionObjectTypes)
            constraintTypes.Add(exclusion);
        var targetTypes = config.Rules.Select(rule => rule.TargetObjectType)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        if (config.ProtectExistingVegetation)
        {
            targetTypes.Add("existing_tree");
            targetTypes.Add("existing_tree_belt");
        }
        var utilityTargetTypes = targetTypes.Where(UtilityTypes.Contains)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var normalizedTargetTypes = targetTypes.Where(target => !UtilityTypes.Contains(target))
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var constraints = _reader.ReadByObjectType(
            Path.Combine(dataDirectory, config.ConstraintMapFile), constraintTypes);
        var normalized = _reader.ReadByObjectType(
            Path.Combine(dataDirectory, config.NormalizedObjectsFile), normalizedTargetTypes);
        var utilityPath = Path.Combine(dataDirectory, config.ReconstructedUtilitiesFile);
        var reconstructed = File.Exists(utilityPath)
            ? _reader.ReadByObjectType(utilityPath, utilityTargetTypes)
            : new Dictionary<string, Geometry>(StringComparer.OrdinalIgnoreCase);
        if (!constraints.TryGetValue(config.BaseAreaObjectType, out var baseArea) || baseArea.IsEmpty)
            throw new InvalidDataException($"Constraint map has no {config.BaseAreaObjectType}");

        var warnings = new List<string>();
        Geometry commonAllowedArea = baseArea.Copy();
        Geometry? existingTreeCenters = null;
        var physicalExclusions = new Dictionary<string, Geometry>(StringComparer.OrdinalIgnoreCase);
        foreach (var exclusionType in config.HardExclusionObjectTypes)
        {
            if (!constraints.TryGetValue(exclusionType, out var exclusion) || exclusion.IsEmpty)
            {
                warnings.Add($"hard exclusion is missing: {exclusionType}");
                continue;
            }
            commonAllowedArea = RobustDifference(commonAllowedArea, exclusion);
            physicalExclusions[exclusionType] = exclusion;
        }
        if (config.ProtectExistingVegetation)
        {
            var maximumExistingTreeClearance =
                (config.ExistingTreeClearanceM ??
                 config.ExistingTreeCanopyRadiusM +
                 config.PlantingProfiles.Max(item => item.FootprintRadiusM)) *
                config.DxfUnitsPerMeter;
            var vegetationScope = commonAllowedArea.Buffer(maximumExistingTreeClearance);
            var preparedVegetationScope = PreparedGeometryFactory.Prepare(vegetationScope);
            if (normalized.TryGetValue("existing_tree", out var existingTrees) && !existingTrees.IsEmpty)
            {
                var localTrees = FilterByPreparedArea(existingTrees, preparedVegetationScope);
                if (!localTrees.IsEmpty)
                {
                    existingTreeCenters = localTrees;
                    // Keep the source centres in the model.  The configured
                    // centre-to-centre clearance is applied per plant profile
                    // and must not receive the new crown radius a second time.
                    physicalExclusions["existing_tree"] = localTrees;
                }
            }
            if (normalized.TryGetValue("existing_tree_belt", out var existingBelts) && !existingBelts.IsEmpty)
            {
                var localBelts = FilterByPreparedArea(existingBelts, preparedVegetationScope);
                if (!localBelts.IsEmpty)
                {
                    commonAllowedArea = RobustDifference(commonAllowedArea, localBelts);
                    physicalExclusions["existing_tree_belt"] = localBelts;
                }
            }
        }
        var plantTypes = new Dictionary<string, PreparedPlantType>(StringComparer.OrdinalIgnoreCase);
        foreach (var profile in config.PlantingProfiles)
        {
            Geometry profileSurface = commonAllowedArea;
            // Keep the mature footprint inside the physical planting surface.
            // Normative distances below are centre/trunk distances and must not
            // receive this radius a second time.
            // A point symbol represents a single mature plant and its full
            // footprint has to fit inside the allowed surface.  An area
            // profile already represents the final planting bed, so eroding
            // that polygon would leave most narrow beds artificially empty.
            var clearance = profile.GeometryKind == "point"
                ? Math.Max(profile.EdgeClearanceM, profile.FootprintRadiusM) *
                  config.DxfUnitsPerMeter
                : 0.0;
            Geometry allowed = clearance > 0
                ? profileSurface.Buffer(-clearance)
                : profileSurface.Copy();
            if (profile.GeometryKind == "point" && existingTreeCenters is not null)
            {
                var existingTreeClearance =
                    (config.ExistingTreeClearanceM ??
                     config.ExistingTreeCanopyRadiusM + profile.FootprintRadiusM) *
                    config.DxfUnitsPerMeter;
                var existingTreeExclusion = existingTreeClearance > 0
                    ? existingTreeCenters.Buffer(existingTreeClearance * 1.001, 32)
                    : existingTreeCenters;
                allowed = RobustDifference(allowed, existingTreeExclusion);
            }
            var evaluations = new List<RuleEvaluation>();
            var reviews = new List<string>();
            foreach (var rule in config.Rules.Where(rule =>
                         rule.PlantType.Equals(profile.PlantType, StringComparison.OrdinalIgnoreCase)))
            {
                var source = "normalized";
                Geometry? target = null;
                if (ConstraintAliases.TryGetValue(rule.TargetObjectType, out var constraintType) &&
                    constraints.TryGetValue(constraintType, out var constraintGeometry))
                {
                    target = constraintGeometry;
                    source = "constraint_map";
                }
                else if (UtilityTypes.Contains(rule.TargetObjectType) &&
                    reconstructed.TryGetValue(rule.TargetObjectType, out var reconstructedGeometry))
                {
                    target = reconstructedGeometry;
                    source = "reconstructed";
                }
                else if (!rule.RequireReconstructedGeometry &&
                         normalized.TryGetValue(rule.TargetObjectType, out var normalizedGeometry))
                {
                    target = normalizedGeometry;
                }

                if (rule.Check == "manual_review")
                {
                    if (target is not null)
                        reviews.Add($"{rule.Code}: {rule.Reason}");
                    evaluations.Add(new RuleEvaluation
                    {
                        Rule = rule,
                        TargetGeometry = target,
                        GeometrySource = source,
                        Status = target is null ? "not_applicable_no_geometry" : "manual_review"
                    });
                    continue;
                }

                if (target is null || target.IsEmpty)
                {
                    var reason = $"{rule.Code}: no trusted {rule.TargetObjectType} geometry";
                    warnings.Add(reason);
                    reviews.Add(reason);
                    evaluations.Add(new RuleEvaluation
                    {
                        Rule = rule,
                        GeometrySource = source,
                        Status = "manual_review_missing_geometry"
                    });
                    continue;
                }

                var distance = rule.MinDistanceM!.Value * config.DxfUnitsPerMeter;
                target = FilterByEnvelope(
                    target,
                    commonAllowedArea.EnvelopeInternal,
                    distance + profile.EdgeClearanceM * config.DxfUnitsPerMeter);
                if (target.IsEmpty)
                {
                    evaluations.Add(new RuleEvaluation
                    {
                        Rule = rule,
                        GeometrySource = source,
                        Status = "not_applicable_outside_work_area"
                    });
                    continue;
                }
                var numericSafety = Math.Max(0.01 * config.DxfUnitsPerMeter, 1e-6);
                allowed = RobustDifference(allowed, target.Buffer(distance + numericSafety));
                evaluations.Add(new RuleEvaluation
                {
                    Rule = rule,
                    TargetGeometry = target,
                    DistanceIndex = new IndexedFacetDistance(target),
                    GeometrySource = source,
                    Status = "applied"
                });
            }

            plantTypes[profile.PlantType] = new PreparedPlantType
            {
                Profile = profile,
                AllowedArea = allowed,
                RuleEvaluations = evaluations,
                ReviewReasons = reviews.Distinct().ToArray()
            };
        }

        return new PreparedModel
        {
            Config = config,
            PlantTypes = plantTypes,
            NormalizedObjects = normalized,
            ReconstructedUtilities = reconstructed,
            BaseArea = baseArea,
            HardExclusions = physicalExclusions,
            HardExclusionDistanceIndexes = physicalExclusions.ToDictionary(
                item => item.Key,
                item => new IndexedFacetDistance(item.Value),
                StringComparer.OrdinalIgnoreCase),
            Warnings = warnings.Distinct().ToArray()
        };
    }

    public IReadOnlyList<PlacementPoint> GenerateInArea(
        PreparedModel model,
        string plantType,
        Geometry requestedArea,
        string pattern,
        double spacingM,
        int maxCount,
        IReadOnlyList<ExistingPlacement>? existingPlacements = null)
    {
        if (!model.PlantTypes.TryGetValue(plantType, out var prepared))
            throw new ArgumentException($"Unknown plant type: {plantType}", nameof(plantType));
        if (prepared.Profile.GeometryKind != "point")
            throw new ArgumentException($"Plant type {plantType} is an area coverage", nameof(plantType));
        if (requestedArea.IsEmpty)
            return Array.Empty<PlacementPoint>();
        EnsureFiniteGeometry(requestedArea, nameof(requestedArea));
        if (!HasPolygonalComponent(requestedArea))
            throw new ArgumentException("Requested area must be polygonal", nameof(requestedArea));
        if (!double.IsFinite(spacingM) || spacingM <= 0)
            throw new ArgumentOutOfRangeException(nameof(spacingM), "Spacing must be positive");
        if (maxCount <= 0)
            return Array.Empty<PlacementPoint>();

        var footprint = prepared.Profile.FootprintRadiusM * model.Config.DxfUnitsPerMeter;
        var safeRequestedArea = footprint > 0 ? requestedArea.Buffer(-footprint) : requestedArea;
        if (safeRequestedArea.IsEmpty)
            return Array.Empty<PlacementPoint>();
        var scope = OverlayNGRobust.Overlay(
            prepared.AllowedArea,
            safeRequestedArea,
            SpatialFunction.Intersection);
        if (scope.IsEmpty)
            return Array.Empty<PlacementPoint>();

        var spacing = Math.Max(
            spacingM,
            prepared.Profile.FootprintRadiusM * 2.0) * model.Config.DxfUnitsPerMeter;
        // Each disconnected planting patch gets its own best lattice angle and
        // phase.  A single global lattice left avoidable holes because the
        // optimal orientation for a long narrow strip is usually wrong for a
        // neighbouring square or curved patch.
        var best = new List<Coordinate>();
        var fixedPlacements = (existingPlacements ?? Array.Empty<ExistingPlacement>()).ToList();
        foreach (var component in PolygonParts(scope).OrderByDescending(item => item.Area))
        {
            var remaining = maxCount - best.Count;
            if (remaining <= 0)
                break;
            var boundarySafety = Math.Max(0.02 * model.Config.DxfUnitsPerMeter, 1e-5);
            var candidateComponent = component.Buffer(-boundarySafety);
            if (candidateComponent.IsEmpty)
                continue;
            var candidateSets = pattern.Equals("boundary", StringComparison.OrdinalIgnoreCase)
                ? Enumerable.Range(0, 8).Select(index =>
                    BoundaryCandidates(candidateComponent, spacing, index * spacing / 8.0))
                : GridCandidateVariants(candidateComponent, spacing, candidateComponent);
            IReadOnlyList<Coordinate> componentBest = Array.Empty<Coordinate>();
            foreach (var candidates in candidateSets)
            {
                var occupied = fixedPlacements.Concat(best.Select((item, index) =>
                    new ExistingPlacement(plantType, item.X, item.Y, $"accepted-{index}"))).ToArray();
                var packed = PackCandidates(
                    model, prepared.Profile, component, candidates, remaining, occupied);
                if (packed.Count > componentBest.Count)
                    componentBest = packed;
            }
            best.AddRange(componentBest);
        }
        var startNumber = NextPlantNumber(existingPlacements, plantType);
        return best.Select((coordinate, index) => CreatePlacementPoint(
            prepared, coordinate, $"{PlantPrefix(plantType)}-{startNumber + index:0000}",
            model.Config.DxfUnitsPerMeter)).ToArray();
    }

    /// <summary>
    /// Places a regular architectural row along a designer-selected guide.
    /// The row is centred on every guide segment. Candidates that violate the
    /// prepared planting area or spacing to existing plants are omitted.
    /// </summary>
    public IReadOnlyList<PlacementPoint> GenerateAlongGuide(
        PreparedModel model,
        string plantType,
        Geometry guide,
        double spacingM,
        int maxCount,
        IReadOnlyList<ExistingPlacement>? existingPlacements = null)
    {
        if (!model.PlantTypes.TryGetValue(plantType, out var prepared))
            throw new ArgumentException($"Unknown plant type: {plantType}", nameof(plantType));
        if (prepared.Profile.GeometryKind != "point")
            throw new ArgumentException($"Plant type {plantType} is an area coverage", nameof(plantType));
        if (guide.IsEmpty)
            return Array.Empty<PlacementPoint>();
        EnsureFiniteGeometry(guide, nameof(guide));
        if (!HasLinealComponent(guide))
            throw new ArgumentException("Guide must be a LineString or MultiLineString", nameof(guide));
        if (!double.IsFinite(spacingM) || spacingM <= 0)
            throw new ArgumentOutOfRangeException(nameof(spacingM), "Spacing must be positive");
        if (maxCount <= 0)
            return Array.Empty<PlacementPoint>();

        var spacing = Math.Max(spacingM, prepared.Profile.FootprintRadiusM * 2.0) *
                      model.Config.DxfUnitsPerMeter;
        var candidates = new List<Coordinate>();
        foreach (var line in LineParts(guide))
        {
            if (line.IsEmpty || line.Length <= 1e-8)
                continue;
            var available = maxCount - candidates.Count;
            if (available <= 0)
                break;
            var count = Math.Min(available, Math.Max(1, (int)Math.Floor(line.Length / spacing) + 1));
            var indexed = new LengthIndexedLine(line);
            if (count == 1)
            {
                candidates.Add(indexed.ExtractPoint(line.Length / 2.0));
                continue;
            }
            var occupiedLength = (count - 1) * spacing;
            var start = Math.Max(0.0, (line.Length - occupiedLength) / 2.0);
            for (var index = 0; index < count; index++)
                candidates.Add(indexed.ExtractPoint(start + index * spacing));
        }

        var validCandidates = candidates.Where(coordinate =>
            prepared.AllowedArea.Covers(_geometryFactory.CreatePoint(coordinate)));
        var accepted = PackCandidates(
            model,
            prepared.Profile,
            prepared.AllowedArea,
            validCandidates,
            maxCount,
            existingPlacements ?? Array.Empty<ExistingPlacement>());
        var startNumber = NextPlantNumber(existingPlacements, plantType);
        return accepted.Select((coordinate, index) => CreatePlacementPoint(
            prepared,
            coordinate,
            $"{PlantPrefix(plantType)}-{startNumber + index:0000}",
            model.Config.DxfUnitsPerMeter)).ToArray();
    }

    public IReadOnlyList<PlacementPoint> Generate(
        PreparedModel model,
        string scenario = "balanced")
    {
        var accepted = new List<PlacementPoint>();
        var pointTypes = model.PlantTypes.Values
            .Where(item => item.Profile.GeometryKind == "point")
            .ToArray();
        IEnumerable<PreparedPlantType> selectedTypes = scenario.ToLowerInvariant() switch
        {
            "maximum_trees" => pointTypes.Where(item =>
                item.Profile.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase)),
            "maximum_plants" => pointTypes.OrderBy(item => item.Profile.FootprintRadiusM)
                .ThenBy(item => item.Profile.SpacingM).Take(1),
            "grass_only" => Array.Empty<PreparedPlantType>(),
            "dense_balanced" => pointTypes.OrderByDescending(item => item.Profile.FootprintRadiusM)
                .ThenByDescending(item => item.Profile.SpacingM),
            "balanced" => pointTypes.OrderByDescending(item => item.Profile.FootprintRadiusM)
                .ThenByDescending(item => item.Profile.SpacingM),
            _ => throw new ArgumentException($"Unknown planting scenario: {scenario}", nameof(scenario))
        };
        // Larger plantings go first.  Smaller types then occupy the remaining
        // valid gaps without invalidating the tree plan.
        foreach (var prepared in selectedTypes)
        {
            if (prepared.AllowedArea.IsEmpty)
                continue;
            var existing = accepted.Select((item, index) => new ExistingPlacement(
                item.PlantType, item.X, item.Y, item.Id.Length > 0 ? item.Id : $"generated-{index}"))
                .ToArray();
            var generated = GenerateInArea(
                model,
                prepared.Profile.PlantType,
                model.BaseArea,
                "grid",
                scenario.Equals("balanced", StringComparison.OrdinalIgnoreCase)
                    ? prepared.Profile.SpacingM * 1.2
                    : prepared.Profile.SpacingM,
                prepared.Profile.MaxCount ?? model.Config.MaxPlacementsPerType,
                existing);
            accepted.AddRange(generated);
        }
        return accepted;
    }

    public IReadOnlyList<PlantingArea> GenerateCoverageAreas(
        PreparedModel model,
        Geometry requestedArea,
        IReadOnlyList<PlacementPoint>? pointPlantings = null,
        string scenario = "full")
    {
        if (requestedArea.IsEmpty)
            return Array.Empty<PlantingArea>();
        EnsureFiniteGeometry(requestedArea, nameof(requestedArea));
        if (!HasPolygonalComponent(requestedArea))
            throw new ArgumentException("Requested area must be polygonal", nameof(requestedArea));
        var result = new List<PlantingArea>();
        Geometry occupiedArea = _geometryFactory.CreateGeometryCollection();
        var scenarioCode = scenario.ToLowerInvariant();
        foreach (var prepared in model.PlantTypes.Values.Where(item =>
                     item.Profile.GeometryKind == "area" &&
                     (scenarioCode != "grass_only" || item.Profile.PlantType.Equals(
                         "herbaceous", StringComparison.OrdinalIgnoreCase)) &&
                     (scenarioCode != "maximum_trees" || item.Profile.PlantType.Equals(
                         "herbaceous", StringComparison.OrdinalIgnoreCase)))
                 .OrderBy(item => item.Profile.PlantType.Equals(
                     "herbaceous", StringComparison.OrdinalIgnoreCase) ? 1 : 0))
        {
            var geometry = OverlayNGRobust.Overlay(
                prepared.AllowedArea,
                requestedArea,
                SpatialFunction.Intersection);
            // A mixed plan uses shrubs as deliberate border beds and keeps the
            // central field readable for trees and lawn.  A shrub-only request
            // still receives the complete allowed polygon through the default
            // "full" mode.
            if ((scenarioCode == "balanced" || scenarioCode == "dense_balanced") &&
                prepared.Profile.PlantType.Equals("shrub", StringComparison.OrdinalIgnoreCase))
            {
                var bandWidth = Math.Max(
                    1.5,
                    prepared.Profile.FootprintRadiusM * 2.0) *
                    model.Config.DxfUnitsPerMeter;
                geometry = OverlayNGRobust.Overlay(
                    geometry,
                    requestedArea.Boundary.Buffer(bandWidth),
                    SpatialFunction.Intersection);
                if (pointPlantings is not null)
                {
                    var treeFootprints = pointPlantings
                        .Where(item => item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase))
                        .Select(item => _geometryFactory.CreatePoint(new Coordinate(item.X, item.Y))
                            .Buffer((item.FootprintRadiusM + 0.25) * model.Config.DxfUnitsPerMeter, 16))
                        .ToArray();
                    if (treeFootprints.Length > 0)
                        geometry = RobustDifference(
                            geometry,
                            _geometryFactory.BuildGeometry(treeFootprints).Union());
                }
            }
            if (!occupiedArea.IsEmpty)
                geometry = RobustDifference(geometry, occupiedArea);
            var minimumArea = Math.Max(0.05, prepared.Profile.SymbolRadiusM * prepared.Profile.SymbolRadiusM) *
                              model.Config.DxfUnitsPerMeter * model.Config.DxfUnitsPerMeter;
            var number = 1;
            foreach (var polygon in PolygonParts(geometry).Where(item => item.Area >= minimumArea))
            {
                var point = polygon.PointOnSurface;
                var checks = BuildRuleChecks(prepared, point, model.Config.DxfUnitsPerMeter).ToList();
                checks.Insert(0, new RuleCheckResult(
                    "AREA_COVERAGE", "подтверждённая озеленяемая поверхность", "passed",
                    null, null, "Геометрическая проверка исходных ограничений",
                    "Полигон находится внутри подтверждённой области и не пересекает дороги, тротуары, здания, твёрдые покрытия и подтверждённые инженерные объекты."));
                result.Add(new PlantingArea(
                    $"{PlantPrefix(prepared.Profile.PlantType)}-{number++:0000}",
                    prepared.Profile.PlantType,
                    prepared.Profile.Species,
                    prepared.ReviewReasons.Count == 0 ? "accepted" : "manual_review",
                    polygon,
                    prepared.RuleEvaluations.Where(item => item.Status == "applied")
                        .Select(item => item.Rule.Code).ToArray(),
                    prepared.ReviewReasons,
                    checks));
            }
            if (!geometry.IsEmpty)
                occupiedArea = occupiedArea.IsEmpty
                    ? geometry.Copy()
                    : OverlayNGRobust.Overlay(occupiedArea, geometry, SpatialFunction.Union);
        }
        return result;
    }

    public IReadOnlyList<PlacementValidation> Validate(
        PreparedModel model,
        IReadOnlyList<ExistingPlacement> placements)
    {
        var result = new List<PlacementValidation>();
        var maximumSpacing = model.Config.PlantingProfiles
            .Where(item => item.GeometryKind == "point")
            .Select(item => Math.Max(item.SpacingM,
                Math.Max(item.AvoidOtherPlantingsM, item.FootprintRadiusM * 2.0)))
            .DefaultIfEmpty(1.0).Max() * model.Config.DxfUnitsPerMeter;
        var spacingCellSize = Math.Max(maximumSpacing, 0.001);
        var placementGrid = new Dictionary<(long X, long Y), List<ExistingPlacement>>();
        foreach (var item in placements)
        {
            if (!double.IsFinite(item.X) || !double.IsFinite(item.Y))
                continue;
            var key = ((long)Math.Floor(item.X / spacingCellSize),
                (long)Math.Floor(item.Y / spacingCellSize));
            if (!placementGrid.TryGetValue(key, out var items))
                placementGrid[key] = items = new List<ExistingPlacement>();
            items.Add(item);
        }
        foreach (var placement in placements)
        {
            var errors = new List<string>();
            var reviews = new List<string>();
            var checks = new List<RuleCheckResult>();
            if (!double.IsFinite(placement.X) || !double.IsFinite(placement.Y))
            {
                errors.Add("Координаты посадки должны быть конечными числами.");
                checks.Add(new RuleCheckResult(
                    "FINITE_COORDINATES", "координаты посадки", "failed", null, null,
                    "Контракт входных данных GreenAI",
                    "Координаты X/Y содержат NaN или Infinity; объект нельзя проверить или записать в DXF."));
                result.Add(new PlacementValidation(
                    placement, false, errors, reviews, checks));
                continue;
            }
            if (!model.PlantTypes.TryGetValue(placement.PlantType, out var prepared))
            {
                errors.Add("Unknown plant type");
            }
            else
            {
                var point = _geometryFactory.CreatePoint(new Coordinate(placement.X, placement.Y));
                var radius = prepared.Profile.FootprintRadiusM * model.Config.DxfUnitsPerMeter;
                var footprint = radius > 0 ? point.Buffer(radius) : point;
                var inside = model.BaseArea.Covers(footprint);
                checks.Add(new RuleCheckResult(
                    "WITHIN_CONFIRMED_AREA", "подтверждённая область", inside ? "passed" : "failed",
                    null, prepared.Profile.FootprintRadiusM, "Геометрическая проверка",
                    inside
                        ? $"Полный габарит посадки радиусом {prepared.Profile.FootprintRadiusM:F2} м находится внутри контура."
                        : $"Габарит радиусом {prepared.Profile.FootprintRadiusM:F2} м выходит за подтверждённый контур."));
                if (!inside)
                    errors.Add("Полный габарит посадки выходит за подтверждённую область озеленения.");
                if (placement.RequiredArea is not null)
                {
                    var insideRequiredArea = placement.RequiredArea.Covers(footprint);
                    var areaSource = string.IsNullOrWhiteSpace(placement.RequiredAreaSource)
                        ? "выбранный пользователем контур"
                        : placement.RequiredAreaSource;
                    checks.Add(new RuleCheckResult(
                        "WITHIN_SELECTED_ZONE", areaSource,
                        insideRequiredArea ? "passed" : "failed",
                        null, prepared.Profile.FootprintRadiusM,
                        "Граница зоны, выбранная проектировщиком",
                        insideRequiredArea
                            ? $"Полный габарит посадки радиусом {prepared.Profile.FootprintRadiusM:F2} м " +
                              $"находится внутри зоны «{areaSource}»."
                            : $"Полный габарит посадки радиусом {prepared.Profile.FootprintRadiusM:F2} м " +
                              $"выходит за зону «{areaSource}»."));
                    if (!insideRequiredArea)
                        errors.Add($"Полный габарит посадки выходит за зону «{areaSource}».");
                }
                foreach (var exclusion in model.HardExclusions)
                {
                    if (exclusion.Key.Equals("existing_tree", StringComparison.OrdinalIgnoreCase))
                    {
                        var requiredDistanceM = model.Config.ExistingTreeClearanceM ??
                                                model.Config.ExistingTreeCanopyRadiusM +
                                                prepared.Profile.FootprintRadiusM;
                        var existingTreeActualDistance = model.HardExclusionDistanceIndexes[exclusion.Key]
                            .Distance(point);
                        var existingTreeActualDistanceM = existingTreeActualDistance /
                                                          model.Config.DxfUnitsPerMeter;
                        var existingTreeClear = existingTreeActualDistanceM + 1e-7 >= requiredDistanceM;
                        checks.Add(new RuleCheckResult(
                            "EXISTING_TREE_CLEARANCE", "существующего дерева",
                            existingTreeClear ? "passed" : "failed", existingTreeActualDistanceM,
                            requiredDistanceM,
                            "Проектное геометрическое требование сохранения существующей растительности",
                            $"До ближайшего существующего дерева {existingTreeActualDistanceM:F2} м; " +
                            $"принятый защитный интервал — {requiredDistanceM:F2} м."));
                        if (!existingTreeClear)
                            errors.Add(
                                $"До существующего дерева {existingTreeActualDistanceM:F2} м при требуемом " +
                                $"интервале {requiredDistanceM:F2} м.");
                        continue;
                    }
                    // Tangency is permitted; only an actual overlap of the
                    // mature footprint with the exclusion is an error.
                    var actualDistance = model.HardExclusionDistanceIndexes[exclusion.Key].Distance(point);
                    var clear = actualDistance + 1e-7 >= radius;
                    var actualDistanceM = actualDistance / model.Config.DxfUnitsPerMeter;
                    checks.Add(new RuleCheckResult(
                        $"NO_{exclusion.Key.ToUpperInvariant()}", ConstraintTitle(exclusion.Key),
                        clear ? "passed" : "failed", actualDistanceM,
                        prepared.Profile.FootprintRadiusM, "Геометрическая проверка",
                        clear
                            ? $"До {ConstraintTitle(exclusion.Key)} {actualDistanceM:F2} м; " +
                              $"габарит радиусом {prepared.Profile.FootprintRadiusM:F2} м не пересекается с ограничением."
                            : $"До {ConstraintTitle(exclusion.Key)} {actualDistanceM:F2} м; " +
                              $"габарит радиусом {prepared.Profile.FootprintRadiusM:F2} м пересекает ограничение."));
                    if (!clear)
                        errors.Add($"Посадка попадает в запрещённую зону: {ConstraintTitle(exclusion.Key)}.");
                }
                reviews.AddRange(prepared.ReviewReasons);
                var ruleChecks = BuildRuleChecks(prepared, point, model.Config.DxfUnitsPerMeter);
                checks.AddRange(ruleChecks);
                errors.AddRange(ruleChecks.Where(item => item.Status == "failed")
                    .Select(item => item.Explanation));
                if (!prepared.AllowedArea.Covers(point) && errors.Count == 0)
                    errors.Add(
                        $"Контур посадки не помещается в разрешённую зону: нужен отступ " +
                        $"{Math.Max(prepared.Profile.EdgeClearanceM, prepared.Profile.FootprintRadiusM):F2} м от её границы.");
                var cellX = (long)Math.Floor(placement.X / spacingCellSize);
                var cellY = (long)Math.Floor(placement.Y / spacingCellSize);
                var spacingFailed = false;
                for (var offsetX = -1; offsetX <= 1 && !spacingFailed; offsetX++)
                for (var offsetY = -1; offsetY <= 1 && !spacingFailed; offsetY++)
                {
                    if (!placementGrid.TryGetValue((cellX + offsetX, cellY + offsetY), out var neighbours))
                        continue;
                    foreach (var other in neighbours)
                    {
                        if (ReferenceEquals(other, placement) || other.SourceId == placement.SourceId)
                            continue;
                        if (!model.PlantTypes.TryGetValue(other.PlantType, out var otherPrepared))
                            continue;
                        var required = RequiredSpacing(prepared.Profile, otherPrepared.Profile) *
                                       model.Config.DxfUnitsPerMeter;
                        if (required <= 0)
                            continue;
                        var distance = Math.Sqrt(Math.Pow(other.X - placement.X, 2) + Math.Pow(other.Y - placement.Y, 2));
                        if (distance + 1e-7 >= required)
                            continue;
                        errors.Add(
                            $"Недостаточный шаг до другой посадки: " +
                            $"{distance / model.Config.DxfUnitsPerMeter:F2} м, " +
                            $"требуется {required / model.Config.DxfUnitsPerMeter:F2} м.");
                        checks.Add(new RuleCheckResult(
                            "PLANT_SPACING", "другая посадка", "failed",
                            distance / model.Config.DxfUnitsPerMeter,
                            required / model.Config.DxfUnitsPerMeter,
                            "Параметры выбранных растений",
                            errors[^1]));
                        spacingFailed = true;
                        break;
                    }
                }
                if (checks.All(item => item.Code != "PLANT_SPACING"))
                    checks.Add(new RuleCheckResult(
                        "PLANT_SPACING", "другие посадки", "passed", null,
                        prepared.Profile.SpacingM, "Параметры выбранных растений",
                        $"Минимальный шаг {prepared.Profile.SpacingM:F2} м соблюдён."));
            }
            result.Add(new PlacementValidation(
                placement, errors.Count == 0, errors.Distinct().ToArray(), reviews.Distinct().ToArray(), checks));
        }
        return result;
    }

    private IEnumerable<IEnumerable<Coordinate>> GridCandidateVariants(
        Geometry geometry,
        double spacing,
        Geometry boundaryGeometry)
    {
        // A hexagonal lattice repeats every 60 degrees.  Ten-degree angular
        // steps and quarter-cell translations are a deterministic search over
        // 96 layouts.  This is still fast for street-scale polygons and fills
        // irregular contours much more completely than the former 8 layouts.
        var phases = new[] { 0.0, 0.25, 0.5, 0.75 };
        for (var angleIndex = 0; angleIndex < 6; angleIndex++)
        foreach (var phaseX in phases)
        foreach (var phaseY in phases)
        {
            var angle = angleIndex * Math.PI / 18.0;
            yield return GridCandidates(geometry, spacing, angle, phaseX, phaseY);
        }

        // On strongly indented outlines a boundary-first pass can occupy a
        // useful perimeter row which no translated infinite lattice reaches.
        if (!boundaryGeometry.IsEmpty)
        {
            foreach (var offsetIndex in Enumerable.Range(0, 4))
            foreach (var angle in new[] { 0.0, Math.PI / 6.0 })
            {
                var offset = offsetIndex * spacing / 4.0;
                yield return BoundaryCandidates(boundaryGeometry, spacing, offset)
                    .Concat(GridCandidates(geometry, spacing, angle, 0.5, 0.5));
            }
        }
    }

    private IEnumerable<Coordinate> GridCandidates(
        Geometry geometry, double spacing, double angle, double phaseX, double phaseY)
    {
        var rowStep = spacing * Math.Sqrt(3.0) / 2.0;
        var cos = Math.Cos(angle);
        var sin = Math.Sin(angle);
        foreach (var polygon in PolygonParts(geometry))
        {
            var preparedGeometry = PreparedGeometryFactory.Prepare(polygon);
            var envelope = polygon.EnvelopeInternal;
            var centerX = (envelope.MinX + envelope.MaxX) / 2.0;
            var centerY = (envelope.MinY + envelope.MaxY) / 2.0;
            var corners = new[]
            {
                new Coordinate(envelope.MinX, envelope.MinY), new Coordinate(envelope.MinX, envelope.MaxY),
                new Coordinate(envelope.MaxX, envelope.MinY), new Coordinate(envelope.MaxX, envelope.MaxY)
            };
            var projections = corners.Select(item =>
            {
                var dx = item.X - centerX;
                var dy = item.Y - centerY;
                return (U: dx * cos + dy * sin, V: -dx * sin + dy * cos);
            }).ToArray();
            var minU = projections.Min(item => item.U) - spacing;
            var maxU = projections.Max(item => item.U) + spacing;
            var minV = projections.Min(item => item.V) - rowStep;
            var maxV = projections.Max(item => item.V) + rowStep;
            var row = 0;
            for (var v = minV + phaseY * rowStep; v <= maxV; v += rowStep, row++)
            {
                var rowOffset = row % 2 == 0 ? 0.0 : spacing / 2.0;
                for (var u = minU + phaseX * spacing + rowOffset; u <= maxU; u += spacing)
                {
                    var coordinate = new Coordinate(
                        centerX + u * cos - v * sin,
                        centerY + u * sin + v * cos);
                    if (preparedGeometry.Covers(_geometryFactory.CreatePoint(coordinate)))
                        yield return coordinate;
                }
            }
        }
    }

    private static IEnumerable<Coordinate> BoundaryCandidates(
        Geometry geometry, double spacing, double startOffset)
    {
        foreach (var line in LineParts(geometry.Boundary))
        {
            var indexed = new LengthIndexedLine(line);
            for (var distance = startOffset; distance < line.Length; distance += spacing)
                yield return indexed.ExtractPoint(distance);
        }
    }

    private static IEnumerable<LineString> LineParts(Geometry geometry)
    {
        if (geometry is LineString line)
        {
            yield return line;
            yield break;
        }
        if (geometry is not GeometryCollection)
            yield break;
        for (var index = 0; index < geometry.NumGeometries; index++)
        foreach (var part in LineParts(geometry.GetGeometryN(index)))
            yield return part;
    }

    private IReadOnlyList<Coordinate> PackCandidates(
        PreparedModel model,
        PlantingProfile profile,
        Geometry scope,
        IEnumerable<Coordinate> candidates,
        int maxCount,
        IReadOnlyList<ExistingPlacement> existing)
    {
        var accepted = new List<Coordinate>();
        var maximumSpacing = model.Config.PlantingProfiles
            .Where(item => item.GeometryKind == "point")
            .Select(item => Math.Max(
                Math.Max(item.SpacingM, item.AvoidOtherPlantingsM),
                item.FootprintRadiusM + profile.FootprintRadiusM))
            .DefaultIfEmpty(profile.SpacingM)
            .Max() * model.Config.DxfUnitsPerMeter;
        var cellSize = Math.Max(maximumSpacing, 0.001);
        var grid = new Dictionary<(long X, long Y), List<(string Type, double X, double Y)>>();
        void Add(string type, double x, double y)
        {
            var key = ((long)Math.Floor(x / cellSize), (long)Math.Floor(y / cellSize));
            if (!grid.TryGetValue(key, out var items))
                grid[key] = items = new List<(string, double, double)>();
            items.Add((type, x, y));
        }
        foreach (var item in existing)
            Add(item.PlantType, item.X, item.Y);
        foreach (var coordinate in candidates)
        {
            if (accepted.Count >= maxCount)
                break;
            if (!double.IsFinite(coordinate.X) || !double.IsFinite(coordinate.Y))
                continue;
            var point = _geometryFactory.CreatePoint(coordinate);
            if (!scope.Covers(point))
                continue;
            var valid = true;
            var cellX = (long)Math.Floor(coordinate.X / cellSize);
            var cellY = (long)Math.Floor(coordinate.Y / cellSize);
            for (var offsetX = -1; offsetX <= 1 && valid; offsetX++)
            for (var offsetY = -1; offsetY <= 1 && valid; offsetY++)
            {
                if (!grid.TryGetValue((cellX + offsetX, cellY + offsetY), out var neighbours))
                    continue;
                foreach (var other in neighbours)
                {
                    if (!model.PlantTypes.TryGetValue(other.Type, out var otherPrepared))
                        continue;
                    var required = RequiredSpacing(profile, otherPrepared.Profile) *
                                   model.Config.DxfUnitsPerMeter;
                    var dx = coordinate.X - other.X;
                    var dy = coordinate.Y - other.Y;
                    if (dx * dx + dy * dy + 1e-7 < required * required)
                    {
                        valid = false;
                        break;
                    }
                }
            }
            if (!valid)
                continue;
            accepted.Add(coordinate.Copy());
            Add(profile.PlantType, coordinate.X, coordinate.Y);
        }
        return accepted;
    }

    private static double RequiredSpacing(PlantingProfile first, PlantingProfile second)
    {
        var footprintSpacing = first.FootprintRadiusM + second.FootprintRadiusM;
        if (first.PlantType.Equals(second.PlantType, StringComparison.OrdinalIgnoreCase))
            return Math.Max(first.SpacingM, footprintSpacing);
        return Math.Max(Math.Max(first.AvoidOtherPlantingsM, second.AvoidOtherPlantingsM), footprintSpacing);
    }

    private static int NextPlantNumber(
        IReadOnlyList<ExistingPlacement>? existingPlacements,
        string plantType)
    {
        var prefix = PlantPrefix(plantType) + "-";
        var maximum = 0;
        foreach (var item in existingPlacements ?? Array.Empty<ExistingPlacement>())
        {
            if (!item.PlantType.Equals(plantType, StringComparison.OrdinalIgnoreCase) ||
                !item.SourceId.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                continue;
            if (int.TryParse(item.SourceId[prefix.Length..], out var number))
                maximum = Math.Max(maximum, number);
        }
        return maximum + 1;
    }

    private static PlacementPoint CreatePlacementPoint(
        PreparedPlantType prepared,
        Coordinate coordinate,
        string id,
        double unitsPerMeter)
    {
        var point = prepared.AllowedArea.Factory.CreatePoint(coordinate);
        return new PlacementPoint(
            prepared.Profile.PlantType,
            prepared.Profile.Species,
            coordinate.X,
            coordinate.Y,
            prepared.ReviewReasons.Count == 0 ? "accepted" : "manual_review",
            prepared.RuleEvaluations.Where(item => item.Status == "applied")
                .Select(item => item.Rule.Code).ToArray(),
            prepared.ReviewReasons,
            id,
            prepared.Profile.FootprintRadiusM,
            BuildRuleChecks(prepared, point, unitsPerMeter));
    }

    private static IReadOnlyList<RuleCheckResult> BuildRuleChecks(
        PreparedPlantType prepared,
        Point point,
        double unitsPerMeter)
    {
        var checks = new List<RuleCheckResult>();
        checks.Add(new RuleCheckResult(
            "PLANT_SELECTION", "пригодность выбранного растения", "passed",
            null, null, prepared.Profile.CatalogReference,
            prepared.Profile.SelectionReasons.Count > 0
                ? $"Выбран вид «{prepared.Profile.Species}»: {string.Join("; ", prepared.Profile.SelectionReasons)}. Источник: {prepared.Profile.CatalogReference}."
                : $"Выбран вид «{prepared.Profile.Species}» из каталога проекта. Источник: {prepared.Profile.CatalogReference}."));
        foreach (var evaluation in prepared.RuleEvaluations)
        {
            if (evaluation.Status == "applied" && evaluation.TargetGeometry is not null)
            {
                var actualM = evaluation.DistanceIndex!.Distance(point) / unitsPerMeter;
                var requiredM = evaluation.Rule.MinDistanceM!.Value;
                var passed = actualM + 1e-5 >= requiredM;
                checks.Add(new RuleCheckResult(
                    evaluation.Rule.Code,
                    ConstraintTitle(evaluation.Rule.TargetObjectType),
                    passed ? "passed" : "failed",
                    actualM,
                    requiredM,
                    evaluation.Rule.NormReference,
                    passed
                        ? $"До {ConstraintTitle(evaluation.Rule.TargetObjectType)} {actualM:F2} м; минимум {requiredM:F2} м соблюдён. Норма: {evaluation.Rule.NormReference}."
                        : $"{evaluation.Rule.Code}: до {ConstraintTitle(evaluation.Rule.TargetObjectType)} {actualM:F4} м, требуется не менее {requiredM:F2} м. Норма: {evaluation.Rule.NormReference}."));
            }
            else if (evaluation.Status == "manual_review")
            {
                checks.Add(new RuleCheckResult(
                    evaluation.Rule.Code,
                    ConstraintTitle(evaluation.Rule.TargetObjectType),
                    "manual_review", null, evaluation.Rule.MinDistanceM,
                    evaluation.Rule.NormReference,
                    $"Требуется ручная проверка: {evaluation.Rule.Reason} Норма: {evaluation.Rule.NormReference}."));
            }
        }
        return checks;
    }

    private static string PlantPrefix(string plantType) => plantType switch
    {
        "tree" => "T",
        "shrub" => "S",
        "herbaceous" => "H",
        "groundcover" => "G",
        _ => "P"
    };

    private static string ConstraintTitle(string objectType) => objectType switch
    {
        "building" or "buildings_in_work_area" => "здания",
        "sidewalk" or "sidewalk_area" => "тротуара",
        "road_edge" or "road_area" => "проезжей части",
        "hard_surface_area" => "твёрдого покрытия",
        "utility_well_footprints" => "инженерного колодца",
        "existing_tree" => "существующего дерева",
        "existing_tree_belt" => "существующей древесной полосы",
        "gas_pipe" => "газопровода",
        "water_pipe" => "водопровода",
        "heat_pipe" => "теплосети",
        "power_cable" => "силового кабеля",
        "overhead_power_line" => "ЛЭП",
        _ => objectType
    };

    private static void EnsureFiniteGeometry(Geometry geometry, string parameterName)
    {
        if (geometry.Coordinates.Any(item => !double.IsFinite(item.X) || !double.IsFinite(item.Y)))
            throw new ArgumentException("Geometry contains NaN or Infinity coordinates", parameterName);
    }

    private static bool HasPolygonalComponent(Geometry geometry)
    {
        if (geometry is Polygon or MultiPolygon)
            return true;
        if (geometry is not GeometryCollection)
            return false;
        for (var index = 0; index < geometry.NumGeometries; index++)
            if (HasPolygonalComponent(geometry.GetGeometryN(index)))
                return true;
        return false;
    }

    private static bool HasLinealComponent(Geometry geometry)
    {
        if (geometry is LineString or MultiLineString)
            return true;
        if (geometry is not GeometryCollection)
            return false;
        for (var index = 0; index < geometry.NumGeometries; index++)
            if (HasLinealComponent(geometry.GetGeometryN(index)))
                return true;
        return false;
    }

    private static IEnumerable<Polygon> PolygonParts(Geometry geometry)
    {
        if (geometry is Polygon polygon)
        {
            yield return polygon;
            yield break;
        }
        if (geometry is not GeometryCollection)
            yield break;
        for (var index = 0; index < geometry.NumGeometries; index++)
        {
            foreach (var part in PolygonParts(geometry.GetGeometryN(index)))
                yield return part;
        }
    }

    private Geometry FilterByEnvelope(Geometry geometry, Envelope scope, double margin)
    {
        var expanded = new Envelope(scope);
        expanded.ExpandBy(margin);
        var parts = new List<Geometry>();
        CollectIntersectingParts(geometry, expanded, parts);
        return parts.Count switch
        {
            0 => _geometryFactory.CreateGeometryCollection(),
            1 => parts[0],
            _ => _geometryFactory.BuildGeometry(parts)
        };
    }

    private Geometry FilterByPreparedArea(Geometry geometry, IPreparedGeometry scope)
    {
        var parts = new List<Geometry>();
        void Collect(Geometry item)
        {
            if (item is GeometryCollection)
            {
                for (var index = 0; index < item.NumGeometries; index++)
                    Collect(item.GetGeometryN(index));
                return;
            }
            if (scope.Intersects(item))
                parts.Add(item);
        }
        Collect(geometry);
        return parts.Count switch
        {
            0 => _geometryFactory.CreateGeometryCollection(),
            1 => parts[0],
            _ => _geometryFactory.BuildGeometry(parts)
        };
    }

    private static void CollectIntersectingParts(
        Geometry geometry,
        Envelope scope,
        ICollection<Geometry> output)
    {
        if (!geometry.EnvelopeInternal.Intersects(scope))
            return;
        if (geometry is GeometryCollection)
        {
            for (var index = 0; index < geometry.NumGeometries; index++)
                CollectIntersectingParts(geometry.GetGeometryN(index), scope, output);
            return;
        }
        output.Add(geometry);
    }

    private static Geometry RobustDifference(Geometry source, Geometry exclusion) =>
        OverlayNGRobust.Overlay(source, exclusion, SpatialFunction.Difference);
}
