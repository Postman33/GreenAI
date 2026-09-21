using System.Reflection;
using System.Text.Json;
using GreenAI.Core;
using HostMgd.ApplicationServices;
using HostMgd.EditorInput;
using Teigha.Colors;
using Teigha.DatabaseServices;
using Teigha.Geometry;
using Teigha.Runtime;
using CadColor = Teigha.Colors.Color;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

public sealed partial class Commands
{
    internal const string MetadataApplication = "GREENAI";
    internal const string BatchMetadataApplication = "GREEN_AI";
    internal const string ReviewLayer = "GREEN_AI_REVIEW";
    internal const string PreviewLayer = "GREEN_AI_PREVIEW";
    private readonly PlacementEngine _engine = new();

    internal static void WarmupForDrawing(string drawingPath)
    {
        var (config, configPath, dataDirectory) = LoadContext(drawingPath);
        PreparedModelCache.Get(new PlacementEngine(), config, configPath, dataDirectory);
    }

    [CommandMethod("GREENAI_PANEL")]
    public void Panel()
    {
        GreenAiPanel.Show();
    }

    [CommandMethod("GREENAI_RUN")]
    public void Run()
    {
        Execute((document, editor, config, configPath, dataDirectory) =>
        {
            GreenAiSession.ResetPreview();
            GreenAiPanel.PreviewUnavailable();
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var run = _engine.Run(model, dataDirectory, GreenAiSession.Scenario);
            var reportPath = Path.Combine(dataDirectory, config.ReportFile);
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                foreach (var profile in config.PlantingProfiles)
                {
                    EnsureLayer(document.Database, transaction, profile.Layer, profile.ColorIndex);
                    EraseLayerEntities(modelSpace, transaction, profile.Layer);
                }
                EnsureLayer(document.Database, transaction, ReviewLayer, 6);
                EraseLayerEntities(modelSpace, transaction, ReviewLayer);
                EnsureLayer(document.Database, transaction, PreviewLayer, 4);
                EraseLayerEntities(modelSpace, transaction, PreviewLayer);

                foreach (var placement in run.Placements)
                {
                    var profile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                        placement.PlantType, StringComparison.OrdinalIgnoreCase));
                    AddCircle(modelSpace, transaction, profile.Layer, profile.ColorIndex,
                        placement.X, placement.Y,
                        profile.SymbolRadiusM * config.DxfUnitsPerMeter,
                        placement);
                }
                foreach (var area in run.CoverageAreas)
                {
                    var profile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                        area.PlantType, StringComparison.OrdinalIgnoreCase));
                    AddPlantingArea(modelSpace, transaction, profile.Layer, profile.ColorIndex, area);
                }
                transaction.Commit();
            }
            // Writing the report after Commit makes its existence evidence that
            // both the calculation and CAD database transaction succeeded.
            ReportWriter.WriteRun(run, reportPath);
            GreenAiSession.LastResultCoordinates = run.Placements
                .Select(item => new NetTopologySuite.Geometries.Coordinate(item.X, item.Y))
                .Concat(run.CoverageAreas.SelectMany(item => item.Geometry.Coordinates))
                .Select(item => item.Copy())
                .ToArray();
            editor.WriteMessage(
                $"\nGreenAI: placed {run.Placements.Count} objects. Report: {reportPath}");
            foreach (var group in run.Placements.GroupBy(item => item.PlantType))
                editor.WriteMessage($"\n  {group.Key}: {group.Count()}");
            if (run.Model.Warnings.Count > 0)
                editor.WriteMessage($"\n  warnings: {run.Model.Warnings.Count}; see report");
            ZoomToCoordinates(editor, run.Placements.Select(item =>
                new NetTopologySuite.Geometries.Coordinate(item.X, item.Y)));
            GreenAiPanel.PlanReady(
                run.Placements.Count(item => item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase)),
                run.Placements.Count(item => item.PlantType.Equals("shrub", StringComparison.OrdinalIgnoreCase)),
                run.CoverageAreas.Sum(item => item.Geometry.Area) /
                (config.DxfUnitsPerMeter * config.DxfUnitsPerMeter),
                run.Scenario,
                run.Model.Warnings.Count,
                run.Model.PlantTypes.Values.Sum(item => item.ReviewReasons.Count));
            foreach (var warning in run.Model.Warnings)
                GreenAiPanel.Log(warning, true);
        });
    }

    [CommandMethod("GREENAI_DATA_QUALITY")]
    public void DataQuality()
    {
        Execute((document, editor, config, configPath, dataDirectory) =>
        {
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var unitArea = config.DxfUnitsPerMeter * config.DxfUnitsPerMeter;
            var lines = new List<string>
            {
                $"Конфигурация: {configPath}",
                $"Каталог данных: {dataDirectory}",
                $"Единицы: 1 метр = {config.DxfUnitsPerMeter:G} единиц DXF (явная настройка)",
                $"Подтверждённая исходная площадь: {model.BaseArea.Area / unitArea:F1} м²",
                $"Жёстких типов ограничений найдено: {model.HardExclusions.Count}",
                $"Типов восстановленных инженерных сетей: {model.ReconstructedUtilities.Count}"
            };
            foreach (var plant in model.PlantTypes.Values.OrderBy(item => item.Profile.PlantType))
            {
                lines.Add(
                    $"{plant.Profile.DisplayName}: допустимо {plant.AllowedArea.Area / unitArea:F1} м²; " +
                    $"автопроверок {plant.RuleEvaluations.Count(item => item.Status == "applied")}; " +
                    $"ручных проверок {plant.ReviewReasons.Count}");
            }
            if (model.Warnings.Count == 0)
                lines.Add("Предупреждения входных данных: отсутствуют.");
            else
            {
                lines.Add("ПРЕДУПРЕЖДЕНИЯ:");
                lines.AddRange(model.Warnings.Select(item => "• " + item));
            }
            GreenAiPanel.ShowPassport(
                model.Warnings.Count == 0 ? "Проверка данных пройдена" : "Данные требуют внимания",
                lines,
                model.Warnings.Count > 0);
            GreenAiPanel.SetStatus("Диагностика входных данных завершена.");
            editor.WriteMessage($"\nGreenAI data quality: {model.Warnings.Count} warning(s).");
        });
    }

    [CommandMethod("GREENAI_CHECK")]
    public void Check()
    {
        Execute((document, editor, config, configPath, dataDirectory) =>
        {
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var placements = ReadPlacements(document, config).ToList();
            var validations = _engine.Validate(model, placements);
            var reportPath = Path.Combine(dataDirectory,
                Path.GetFileNameWithoutExtension(config.ReportFile) + ".validation.json");
            ReportWriter.WriteValidation(validations, reportPath);

            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                EnsureLayer(document.Database, transaction, ReviewLayer, 1);
                EraseLayerEntities(modelSpace, transaction, ReviewLayer);
                foreach (var invalid in validations.Where(item => !item.IsValid))
                {
                    AddCircle(modelSpace, transaction, ReviewLayer, 1,
                        invalid.Placement.X, invalid.Placement.Y,
                        1.5 * config.DxfUnitsPerMeter, invalid);
                }
                transaction.Commit();
            }
            editor.WriteMessage(
                $"\nGreenAI check: {validations.Count(item => item.IsValid)}/{validations.Count} valid; " +
                $"{validations.Count(item => !item.IsValid)} invalid. Report: {reportPath}");
            var invalidCount = validations.Count(item => !item.IsValid);
            GreenAiPanel.SetStatus(
                $"Проверка завершена: {validations.Count - invalidCount} корректно, {invalidCount} ошибок.");
            GreenAiPanel.Log($"Проверено {validations.Count} посадок; ошибок: {invalidCount}.", invalidCount > 0);
            if (invalidCount > 0)
                GreenAiPanel.ShowDetails(
                    "Найдены недопустимые посадки",
                    validations.Where(item => !item.IsValid).Take(10)
                        .SelectMany(item => item.Errors).Distinct(),
                    true);
        });
    }

    [CommandMethod("GREENAI_CLEAR")]
    public void Clear()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var editor = document.Editor;
        var (config, _, _) = LoadContext(document.Name);
        var removed = 0;
        using (document.LockDocument())
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
            foreach (var layer in config.PlantingProfiles.Select(item => item.Layer)
                         .Append(ReviewLayer).Append(PreviewLayer))
                removed += EraseLayerEntities(modelSpace, transaction, layer);
            transaction.Commit();
        }
        editor.WriteMessage($"\nGreenAI: removed {removed} generated entities.");
        GreenAiSession.ResetPreview();
        GreenAiSession.LastResultCoordinates = Array.Empty<NetTopologySuite.Geometries.Coordinate>();
        GreenAiPanel.PreviewUnavailable();
        GreenAiPanel.Log($"Удалено объектов GreenAI: {removed}.");
        GreenAiPanel.SetStatus("Результат очищен.");
    }

    [CommandMethod("GREENAI_INFO")]
    public void Info()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var (config, configPath, dataDirectory) = LoadContext(document.Name);
        document.Editor.WriteMessage(
            $"\nGreenAI plugin\n  config: {configPath}\n  data: {dataDirectory}" +
            $"\n  units/m: {config.DxfUnitsPerMeter}\n  profiles: " +
            string.Join(", ", config.PlantingProfiles.Select(item => item.PlantType)));
        GreenAiPanel.ShowDetails(
            "Данные GreenAI",
            new[]
            {
                $"Конфигурация: {configPath}",
                $"Каталог данных: {dataDirectory}",
                $"Единиц DXF на метр: {config.DxfUnitsPerMeter}",
                $"Типы посадок: {string.Join(", ", config.PlantingProfiles.Select(item => item.PlantType))}"
            },
            false);
    }

    [CommandMethod("GREENAI_AUTORUN_SMOKE")]
    public void AutorunSmoke()
    {
        var completionPath = Environment.GetEnvironmentVariable("GREENAI_PLUGIN_AUTORUN_FILE");
        if (string.IsNullOrWhiteSpace(completionPath))
            return;
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var (config, configPath, dataDirectory) = LoadContext(document.Name);
        var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
        var batchMetadataSmoke = RunBatchMetadataSmoke(document, config);
        var contextMenuSmoke = RunContextMenuSmoke(document, config, model);
        var guidePreviewSmoke = RunGuidePreviewContractSmoke(document, config, model);
        Run();
        var editorZoneSmoke = RunEditorZoneSmoke(document, config, model);
        var zoneReplacementSmoke = RunZoneReplacementSmoke(document, config, model);
        var counts = config.PlantingProfiles.ToDictionary(
            item => item.PlantType, _ => 0, StringComparer.OrdinalIgnoreCase);
        var pointMetadataIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var pointMetadataCount = 0;
        var areaMetadataCount = 0;
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
            foreach (ObjectId id in modelSpace)
            {
                if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity)
                    continue;
                var profile = config.PlantingProfiles.FirstOrDefault(item =>
                    item.Layer.Equals(entity.Layer, StringComparison.OrdinalIgnoreCase));
                if (profile is null)
                    continue;
                if (profile.GeometryKind == "point" && entity is Circle)
                {
                    counts[profile.PlantType]++;
                    var metadata = ReadMetadata(entity);
                    if (metadata is not null && !string.IsNullOrWhiteSpace(metadata.Id))
                    {
                        pointMetadataCount++;
                        pointMetadataIds.Add(metadata.Id);
                    }
                }
                else if (profile.GeometryKind == "area" && entity is Hatch)
                {
                    counts[profile.PlantType]++;
                    var metadata = ReadMetadata(entity);
                    if (metadata is not null && !string.IsNullOrWhiteSpace(metadata.Id))
                        areaMetadataCount++;
                }
            }
            transaction.Commit();
        }
        var pointCount = config.PlantingProfiles.Where(item => item.GeometryKind == "point")
            .Sum(item => counts[item.PlantType]);
        var areaCount = config.PlantingProfiles.Where(item => item.GeometryKind == "area")
            .Sum(item => counts[item.PlantType]);
        var result = new
        {
            status = pointCount > 0 && areaCount > 0 &&
                     batchMetadataSmoke.Found && batchMetadataSmoke.Parsed &&
                     batchMetadataSmoke.PointParsed && batchMetadataSmoke.AreaParsed &&
                     pointMetadataCount == pointCount && pointMetadataIds.Count == pointCount &&
                     areaMetadataCount == areaCount &&
                     contextMenuSmoke.PresetApplied &&
                     contextMenuSmoke.ScopeCaptured &&
                     contextMenuSmoke.PreviewGenerated &&
                     editorZoneSmoke.MetadataPersisted && editorZoneSmoke.InsidePassed &&
                     editorZoneSmoke.OutsideRejected &&
                     zoneReplacementSmoke.PreviewGenerated &&
                     zoneReplacementSmoke.ExistingObjectsReplaced &&
                     zoneReplacementSmoke.ResultIdsUnique &&
                     zoneReplacementSmoke.LastResultRemembered &&
                     zoneReplacementSmoke.ResultLayersShown &&
                     zoneReplacementSmoke.ViewCenteredOnLastResult &&
                     guidePreviewSmoke.StalePatternResolved &&
                     guidePreviewSmoke.PreviewContextRecorded &&
                     guidePreviewSmoke.MetadataHasNoZone &&
                     guidePreviewSmoke.AppliedToModelSpace
                ? "passed"
                : "failed",
            generatedAt = DateTimeOffset.Now,
            drawing = document.Name,
            dataDirectory,
            counts,
            pointCount,
            areaCount,
            pointMetadataCount,
            uniquePointIds = pointMetadataIds.Count,
            areaMetadataCount,
            batchMetadataSmoke,
            contextMenuSmoke,
            editorZoneSmoke,
            zoneReplacementSmoke,
            guidePreviewSmoke,
            reportExists = File.Exists(Path.Combine(dataDirectory, config.ReportFile))
        };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(completionPath))!);
        File.WriteAllText(completionPath, JsonSerializer.Serialize(result, new JsonSerializerOptions
        {
            WriteIndented = true
        }));
    }

    private static BatchMetadataSmokeResult RunBatchMetadataSmoke(
        Document document,
        PluginConfig config)
    {
        var resultLayers = config.PlantingProfiles
            .Select(item => item.Layer)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var found = 0;
        var parsed = 0;
        var pointParsed = false;
        var areaParsed = false;
        var sampleId = "";
        using var transaction = document.Database.TransactionManager.StartTransaction();
        var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
        foreach (ObjectId id in modelSpace)
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity ||
                !resultLayers.Contains(entity.Layer))
                continue;
            using var batchData = entity.GetXDataForApplication(BatchMetadataApplication);
            if (batchData is null)
                continue;
            found++;
            var metadata = ReadMetadata(entity);
            if (metadata is null || string.IsNullOrWhiteSpace(metadata.Id) ||
                metadata.Id.StartsWith("id=", StringComparison.OrdinalIgnoreCase) ||
                metadata.PlantType.StartsWith("type=", StringComparison.OrdinalIgnoreCase) ||
                metadata.Species.StartsWith("species=", StringComparison.OrdinalIgnoreCase) ||
                metadata.Status.StartsWith("status=", StringComparison.OrdinalIgnoreCase))
                continue;
            parsed++;
            sampleId = sampleId.Length == 0 ? metadata.Id : sampleId;
            pointParsed |= entity is Circle;
            areaParsed |= entity is Hatch;
        }
        transaction.Commit();
        return new BatchMetadataSmokeResult(
            found > 0,
            parsed == found,
            pointParsed,
            areaParsed,
            found,
            parsed,
            sampleId);
    }

    private ContextMenuSmokeResult RunContextMenuSmoke(
        Document document,
        PluginConfig config,
        PreparedModel model)
    {
        var zoneId = ObjectId.Null;
        var previousZone = GreenAiSession.SelectedZone;
        var previousGuide = GreenAiSession.SelectedGuide;
        var previousZoneHandle = GreenAiSession.SelectedZoneHandle;
        var previousGuideHandle = GreenAiSession.SelectedGuideHandle;
        var previousDocumentName = GreenAiSession.SelectedDocumentName;
        var previousPlantType = GreenAiSession.PlantType;
        var previousPattern = GreenAiSession.Pattern;
        var previousSpacing = GreenAiSession.SpacingM;
        var previousMaxCount = GreenAiSession.MaxCount;
        try
        {
            var prepared = model.PlantTypes.Values.First(item =>
                item.Profile.GeometryKind == "point" &&
                item.Profile.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase));
            var center = prepared.AllowedArea.PointOnSurface.Coordinate;
            var halfSize = (prepared.Profile.FootprintRadiusM + prepared.Profile.SpacingM) *
                           config.DxfUnitsPerMeter;
            string zoneHandle;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                var zone = new Polyline(4) { Layer = "0", Closed = true };
                zone.AddVertexAt(0, new Point2d(center.X - halfSize, center.Y - halfSize), 0, 0, 0);
                zone.AddVertexAt(1, new Point2d(center.X + halfSize, center.Y - halfSize), 0, 0, 0);
                zone.AddVertexAt(2, new Point2d(center.X + halfSize, center.Y + halfSize), 0, 0, 0);
                zone.AddVertexAt(3, new Point2d(center.X - halfSize, center.Y + halfSize), 0, 0, 0);
                modelSpace.AppendEntity(zone);
                transaction.AddNewlyCreatedDBObject(zone, true);
                zoneId = zone.ObjectId;
                zoneHandle = zone.Handle.ToString();
                transaction.Commit();
            }

            document.Editor.SetImpliedSelection(new[] { zoneId });
            GreenAiContextRequest.Pending = new GreenAiContextRequest("tree", 5.0, 20);
            ContextPreview();
            var context = GreenAiSession.PreviewContext;
            return new ContextMenuSmokeResult(
                GreenAiSession.PlantType == "tree" &&
                Math.Abs(GreenAiSession.SpacingM - 5.0) < 1e-9 &&
                GreenAiSession.MaxCount == 20,
                context is not null && context.ScopeKind == "zone" &&
                context.SourceHandle.Equals(zoneHandle, StringComparison.OrdinalIgnoreCase),
                GreenAiSession.Preview.Count > 0,
                "");
        }
        catch (System.Exception error)
        {
            return new ContextMenuSmokeResult(false, false, false, error.Message);
        }
        finally
        {
            GreenAiContextRequest.Pending = null;
            document.Editor.SetImpliedSelection(Array.Empty<ObjectId>());
            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            if (!zoneId.IsNull)
            {
                using (document.LockDocument())
                using (var transaction = document.Database.TransactionManager.StartTransaction())
                {
                    if (transaction.GetObject(zoneId, OpenMode.ForWrite, false) is Entity zone)
                        zone.Erase();
                    transaction.Commit();
                }
            }
            GreenAiSession.SelectedZone = previousZone;
            GreenAiSession.SelectedGuide = previousGuide;
            GreenAiSession.SelectedZoneHandle = previousZoneHandle;
            GreenAiSession.SelectedGuideHandle = previousGuideHandle;
            GreenAiSession.SelectedDocumentName = previousDocumentName;
            GreenAiSession.PlantType = previousPlantType;
            GreenAiSession.Pattern = previousPattern;
            GreenAiSession.SpacingM = previousSpacing;
            GreenAiSession.MaxCount = previousMaxCount;
        }
    }

    private GuidePreviewSmokeResult RunGuidePreviewContractSmoke(
        Document document,
        PluginConfig config,
        PreparedModel model)
    {
        var guideId = ObjectId.Null;
        var appliedCircleId = ObjectId.Null;
        var previousZone = GreenAiSession.SelectedZone;
        var previousGuide = GreenAiSession.SelectedGuide;
        var previousZoneHandle = GreenAiSession.SelectedZoneHandle;
        var previousGuideHandle = GreenAiSession.SelectedGuideHandle;
        var previousDocumentName = GreenAiSession.SelectedDocumentName;
        var previousPlantType = GreenAiSession.PlantType;
        var previousPattern = GreenAiSession.Pattern;
        var previousSpacing = GreenAiSession.SpacingM;
        var previousMaxCount = GreenAiSession.MaxCount;
        try
        {
            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            var profile = config.PlantingProfiles.First(item =>
                item.GeometryKind == "point" &&
                item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase));
            var existing = ReadPlacements(document, config);
            var available = _engine.GenerateInArea(
                model, profile.PlantType, model.BaseArea, "grid", profile.SpacingM, 1, existing);
            if (available.Count == 0)
                return new GuidePreviewSmokeResult(false, false, false, false, "No free point for guide preview");
            var anchor = available[0];
            var halfLength = 0.5 * config.DxfUnitsPerMeter;
            var guide = model.BaseArea.Factory.CreateLineString(new[]
            {
                new NetTopologySuite.Geometries.Coordinate(anchor.X - halfLength, anchor.Y),
                new NetTopologySuite.Geometries.Coordinate(anchor.X + halfLength, anchor.Y)
            });
            string guideHandle;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                var cadGuide = new Teigha.DatabaseServices.Line(
                    new Point3d(anchor.X - halfLength, anchor.Y, 0),
                    new Point3d(anchor.X + halfLength, anchor.Y, 0));
                modelSpace.AppendEntity(cadGuide);
                transaction.AddNewlyCreatedDBObject(cadGuide, true);
                guideId = cadGuide.ObjectId;
                guideHandle = cadGuide.Handle.ToString();
                transaction.Commit();
            }
            GreenAiSession.SelectedZone = null;
            GreenAiSession.SelectedZoneHandle = "—";
            GreenAiSession.SelectedGuide = guide;
            GreenAiSession.SelectedGuideHandle = guideHandle;
            GreenAiSession.SelectedDocumentName = document.Name;
            GreenAiSession.PlantType = profile.PlantType;
            // Deliberately stale UI state: Preview must infer the selected
            // guide and must not ask for a polygonal work zone.
            GreenAiSession.Pattern = "grid";
            GreenAiSession.SpacingM = profile.SpacingM;
            GreenAiSession.MaxCount = 1;
            Preview();

            var stalePatternResolved = GreenAiSession.Preview.Count == 1;
            var previewId = GreenAiSession.Preview.FirstOrDefault()?.Id ?? "";
            var contextRecorded = GreenAiSession.PreviewContext is
            {
                ScopeKind: "guide"
            } context && context.SourceHandle.Equals(guideHandle, StringComparison.OrdinalIgnoreCase);
            var metadataHasNoZone = false;
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
                metadataHasNoZone = modelSpace.Cast<ObjectId>()
                    .Select(id => transaction.GetObject(id, OpenMode.ForRead))
                    .OfType<Circle>()
                    .Where(item => item.Layer.Equals(PreviewLayer, StringComparison.OrdinalIgnoreCase))
                    .Select(ReadMetadata)
                    .Any(item => item is not null && string.IsNullOrWhiteSpace(item.ZoneHandle));
                transaction.Commit();
            }
            ApplyPreview();
            var appliedToModelSpace = false;
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
                foreach (ObjectId id in modelSpace)
                {
                    if (transaction.GetObject(id, OpenMode.ForRead) is not Circle circle)
                        continue;
                    var metadata = ReadMetadata(circle);
                    if (metadata?.Id != previewId || !string.IsNullOrWhiteSpace(metadata.ZoneHandle))
                        continue;
                    appliedCircleId = id;
                    appliedToModelSpace = true;
                    break;
                }
                transaction.Commit();
            }
            return new GuidePreviewSmokeResult(
                stalePatternResolved, contextRecorded, metadataHasNoZone, appliedToModelSpace, "");
        }
        catch (System.Exception error)
        {
            return new GuidePreviewSmokeResult(false, false, false, false, error.Message);
        }
        finally
        {
            try
            {
                ClearPreviewGraphics();
            }
            catch
            {
                // The primary smoke result preserves the original failure.
            }
            if (!guideId.IsNull || !appliedCircleId.IsNull)
            {
                try
                {
                    using (document.LockDocument())
                    using (var transaction = document.Database.TransactionManager.StartTransaction())
                    {
                        foreach (var id in new[] { appliedCircleId, guideId }.Where(item => !item.IsNull))
                        {
                            if (transaction.GetObject(id, OpenMode.ForWrite, false) is Entity entity)
                                entity.Erase();
                        }
                        transaction.Commit();
                    }
                }
                catch
                {
                    // Preserve the primary smoke result.
                }
            }
            GreenAiSession.ResetPreview();
            GreenAiPanel.PreviewUnavailable();
            GreenAiSession.SelectedZone = previousZone;
            GreenAiSession.SelectedGuide = previousGuide;
            GreenAiSession.SelectedZoneHandle = previousZoneHandle;
            GreenAiSession.SelectedGuideHandle = previousGuideHandle;
            GreenAiSession.SelectedDocumentName = previousDocumentName;
            GreenAiSession.PlantType = previousPlantType;
            GreenAiSession.Pattern = previousPattern;
            GreenAiSession.SpacingM = previousSpacing;
            GreenAiSession.MaxCount = previousMaxCount;
        }
    }

    private EditorZoneSmokeResult RunEditorZoneSmoke(
        Document document,
        PluginConfig config,
        PreparedModel model)
    {
        var zoneId = ObjectId.Null;
        var circleId = ObjectId.Null;
        try
        {
            var prepared = model.PlantTypes.Values.First(item =>
                item.Profile.GeometryKind == "point" &&
                item.Profile.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase));
            var center = prepared.AllowedArea.PointOnSurface.Coordinate;
            var halfSize = (prepared.Profile.FootprintRadiusM + prepared.Profile.SpacingM) *
                           config.DxfUnitsPerMeter;
            var coordinates = new[]
            {
                new NetTopologySuite.Geometries.Coordinate(center.X - halfSize, center.Y - halfSize),
                new NetTopologySuite.Geometries.Coordinate(center.X + halfSize, center.Y - halfSize),
                new NetTopologySuite.Geometries.Coordinate(center.X + halfSize, center.Y + halfSize),
                new NetTopologySuite.Geometries.Coordinate(center.X - halfSize, center.Y + halfSize),
                new NetTopologySuite.Geometries.Coordinate(center.X - halfSize, center.Y - halfSize)
            };
            var zoneGeometry = prepared.AllowedArea.Factory.CreatePolygon(coordinates);
            var generated = _engine.GenerateInArea(
                model,
                prepared.Profile.PlantType,
                zoneGeometry,
                "grid",
                prepared.Profile.SpacingM,
                1,
                Array.Empty<ExistingPlacement>());
            if (generated.Count == 0)
                return new EditorZoneSmokeResult(false, false, false, "No selected-zone point generated");
            var testPlacement = generated[0] with { Id = "EDITOR-ZONE-SMOKE" };
            string zoneHandle;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                EnsureLayer(document.Database, transaction, PreviewLayer, 4);
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                var zone = new Polyline(4)
                {
                    Layer = PreviewLayer,
                    Closed = true
                };
                for (var index = 0; index < 4; index++)
                    zone.AddVertexAt(
                        index,
                        new Point2d(coordinates[index].X, coordinates[index].Y),
                        0, 0, 0);
                modelSpace.AppendEntity(zone);
                transaction.AddNewlyCreatedDBObject(zone, true);
                zoneId = zone.ObjectId;
                zoneHandle = zone.Handle.ToString();
                circleId = AddCircle(
                    modelSpace,
                    transaction,
                    prepared.Profile.Layer,
                    prepared.Profile.ColorIndex,
                    testPlacement.X,
                    testPlacement.Y,
                    prepared.Profile.SymbolRadiusM * config.DxfUnitsPerMeter,
                    testPlacement,
                    zoneHandle);
                transaction.Commit();
            }

            var stored = ReadPlacements(document, config)
                .Single(item => item.SourceId == testPlacement.Id);
            var metadataPersisted = stored.RequiredArea is not null;
            var insideValidation = _engine.Validate(model, new[] { stored })[0];
            var insidePassed = insideValidation.IsValid &&
                               (insideValidation.Checks ?? Array.Empty<RuleCheckResult>()).Any(item =>
                                   item.Code == "WITHIN_SELECTED_ZONE" && item.Status == "passed");

            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var circle = (Circle)transaction.GetObject(circleId, OpenMode.ForWrite);
                circle.Center = new Point3d(
                    center.X + halfSize + prepared.Profile.FootprintRadiusM * config.DxfUnitsPerMeter + 1.0,
                    center.Y,
                    circle.Center.Z);
                transaction.Commit();
            }
            var moved = ReadPlacements(document, config)
                .Single(item => item.SourceId == testPlacement.Id);
            var outsideValidation = _engine.Validate(model, new[] { moved })[0];
            var outsideRejected = !outsideValidation.IsValid &&
                                  (outsideValidation.Checks ?? Array.Empty<RuleCheckResult>()).Any(item =>
                                      item.Code == "WITHIN_SELECTED_ZONE" && item.Status == "failed");
            return new EditorZoneSmokeResult(metadataPersisted, insidePassed, outsideRejected, "");
        }
        catch (System.Exception error)
        {
            return new EditorZoneSmokeResult(false, false, false, error.Message);
        }
        finally
        {
            if (!zoneId.IsNull || !circleId.IsNull)
            {
                using (document.LockDocument())
                using (var transaction = document.Database.TransactionManager.StartTransaction())
                {
                    foreach (var id in new[] { circleId, zoneId }.Where(item => !item.IsNull))
                    {
                        if (transaction.GetObject(id, OpenMode.ForWrite, false) is Entity entity)
                            entity.Erase();
                    }
                    transaction.Commit();
                }
            }
        }
    }

    private ZoneReplacementSmokeResult RunZoneReplacementSmoke(
        Document document,
        PluginConfig config,
        PreparedModel model)
    {
        var zoneId = ObjectId.Null;
        var previousZone = GreenAiSession.SelectedZone;
        var previousGuide = GreenAiSession.SelectedGuide;
        var previousZoneHandle = GreenAiSession.SelectedZoneHandle;
        var previousGuideHandle = GreenAiSession.SelectedGuideHandle;
        var previousDocumentName = GreenAiSession.SelectedDocumentName;
        var previousPlantType = GreenAiSession.PlantType;
        var previousPattern = GreenAiSession.Pattern;
        var previousSpacing = GreenAiSession.SpacingM;
        var previousMaxCount = GreenAiSession.MaxCount;
        try
        {
            var profile = config.PlantingProfiles.First(item =>
                item.GeometryKind == "point" &&
                item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase));
            var anchor = ReadPlacements(document, config).First(item =>
                item.PlantType.Equals(profile.PlantType, StringComparison.OrdinalIgnoreCase));
            var halfSize = (profile.SpacingM + profile.FootprintRadiusM) * config.DxfUnitsPerMeter;
            var coordinates = new[]
            {
                new NetTopologySuite.Geometries.Coordinate(anchor.X - halfSize, anchor.Y - halfSize),
                new NetTopologySuite.Geometries.Coordinate(anchor.X + halfSize, anchor.Y - halfSize),
                new NetTopologySuite.Geometries.Coordinate(anchor.X + halfSize, anchor.Y + halfSize),
                new NetTopologySuite.Geometries.Coordinate(anchor.X - halfSize, anchor.Y + halfSize),
                new NetTopologySuite.Geometries.Coordinate(anchor.X - halfSize, anchor.Y - halfSize)
            };
            var zoneGeometry = model.BaseArea.Factory.CreatePolygon(coordinates);
            string zoneHandle;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                var zone = new Polyline(4) { Layer = "0", Closed = true };
                for (var index = 0; index < 4; index++)
                    zone.AddVertexAt(index, new Point2d(coordinates[index].X, coordinates[index].Y), 0, 0, 0);
                modelSpace.AppendEntity(zone);
                transaction.AddNewlyCreatedDBObject(zone, true);
                zoneId = zone.ObjectId;
                zoneHandle = zone.Handle.ToString();
                transaction.Commit();
            }

            var before = ReadPlacements(document, config);
            var beforeInside = before.Count(item => zoneGeometry.Covers(
                zoneGeometry.Factory.CreatePoint(new NetTopologySuite.Geometries.Coordinate(item.X, item.Y))));
            GreenAiSession.SelectedZone = zoneGeometry;
            GreenAiSession.SelectedGuide = null;
            GreenAiSession.SelectedZoneHandle = zoneHandle;
            GreenAiSession.SelectedGuideHandle = "—";
            GreenAiSession.SelectedDocumentName = document.Name;
            GreenAiSession.PlantType = profile.PlantType;
            GreenAiSession.Pattern = "grid";
            GreenAiSession.SpacingM = profile.SpacingM;
            GreenAiSession.MaxCount = 20;
            Preview();
            var previewGenerated = GreenAiSession.Preview.Count > 0;
            ApplyPreview();

            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var layers = (LayerTable)transaction.GetObject(document.Database.LayerTableId, OpenMode.ForRead);
                var layer = (LayerTableRecord)transaction.GetObject(layers[profile.Layer], OpenMode.ForWrite);
                layer.IsOff = true;
                transaction.Commit();
            }
            ZoomResult();

            var after = ReadPlacements(document, config);
            var afterInside = after.Count(item => zoneGeometry.Covers(
                zoneGeometry.Factory.CreatePoint(new NetTopologySuite.Geometries.Coordinate(item.X, item.Y))));
            var idsUnique = after.Select(item => item.SourceId)
                .Distinct(StringComparer.OrdinalIgnoreCase).Count() == after.Count;
            bool resultLayersShown;
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var layers = (LayerTable)transaction.GetObject(document.Database.LayerTableId, OpenMode.ForRead);
                var layer = (LayerTableRecord)transaction.GetObject(layers[profile.Layer], OpenMode.ForRead);
                resultLayersShown = !layer.IsOff && !layer.IsFrozen;
                transaction.Commit();
            }
            var remembered = GreenAiSession.LastResultCoordinates;
            var expectedX = (remembered.Min(item => item.X) + remembered.Max(item => item.X)) / 2.0;
            var expectedY = (remembered.Min(item => item.Y) + remembered.Max(item => item.Y)) / 2.0;
            using var view = document.Editor.GetCurrentView();
            var viewCentered = Math.Abs(view.CenterPoint.X - expectedX) <= 0.1 &&
                               Math.Abs(view.CenterPoint.Y - expectedY) <= 0.1;
            return new ZoneReplacementSmokeResult(
                previewGenerated,
                beforeInside > 0 && afterInside > 0,
                idsUnique,
                remembered.Count > 0,
                resultLayersShown,
                viewCentered,
                "");
        }
        catch (System.Exception error)
        {
            return new ZoneReplacementSmokeResult(false, false, false, false, false, false, error.Message);
        }
        finally
        {
            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            if (!zoneId.IsNull)
            {
                using (document.LockDocument())
                using (var transaction = document.Database.TransactionManager.StartTransaction())
                {
                    if (transaction.GetObject(zoneId, OpenMode.ForWrite, false) is Entity zone)
                        zone.Erase();
                    transaction.Commit();
                }
            }
            GreenAiSession.SelectedZone = previousZone;
            GreenAiSession.SelectedGuide = previousGuide;
            GreenAiSession.SelectedZoneHandle = previousZoneHandle;
            GreenAiSession.SelectedGuideHandle = previousGuideHandle;
            GreenAiSession.SelectedDocumentName = previousDocumentName;
            GreenAiSession.PlantType = previousPlantType;
            GreenAiSession.Pattern = previousPattern;
            GreenAiSession.SpacingM = previousSpacing;
            GreenAiSession.MaxCount = previousMaxCount;
        }
    }

    private sealed record EditorZoneSmokeResult(
        bool MetadataPersisted,
        bool InsidePassed,
        bool OutsideRejected,
        string Error);

    private sealed record BatchMetadataSmokeResult(
        bool Found,
        bool Parsed,
        bool PointParsed,
        bool AreaParsed,
        int FoundCount,
        int ParsedCount,
        string SampleId);

    private sealed record ContextMenuSmokeResult(
        bool PresetApplied,
        bool ScopeCaptured,
        bool PreviewGenerated,
        string Error);

    private sealed record ZoneReplacementSmokeResult(
        bool PreviewGenerated,
        bool ExistingObjectsReplaced,
        bool ResultIdsUnique,
        bool LastResultRemembered,
        bool ResultLayersShown,
        bool ViewCenteredOnLastResult,
        string Error);

    private sealed record GuidePreviewSmokeResult(
        bool StalePatternResolved,
        bool PreviewContextRecorded,
        bool MetadataHasNoZone,
        bool AppliedToModelSpace,
        string Error);

    private static void Execute(Action<Document, Editor, PluginConfig, string, string> action)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var editor = document.Editor;
        try
        {
            var (config, configPath, dataDirectory) = LoadContext(document.Name);
            action(document, editor, config, configPath, dataDirectory);
        }
        catch (System.Exception error)
        {
            editor.WriteMessage($"\nGreenAI error: {error.Message}\n{error.StackTrace}");
            GreenAiPanel.SetStatus("Не удалось выполнить действие.");
            GreenAiPanel.ShowDetails("Ошибка GreenAI", new[] { error.Message }, true);
        }
    }

    internal static (PluginConfig Config, string ConfigPath, string DataDirectory) LoadContext(string drawingPath)
    {
        var assemblyDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location)!;
        var configPath = Path.Combine(assemblyDirectory, "greenai.plugin.json");
        var config = PluginConfigLoader.Load(configPath);
        var dataDirectory = PluginConfigLoader.ResolveDataDirectory(config, configPath, drawingPath);
        return (config, configPath, dataDirectory);
    }

    internal static BlockTableRecord OpenModelSpace(Database database, Transaction transaction, OpenMode mode)
    {
        var blockTable = (BlockTable)transaction.GetObject(database.BlockTableId, OpenMode.ForRead);
        return (BlockTableRecord)transaction.GetObject(blockTable[BlockTableRecord.ModelSpace], mode);
    }

    internal static void EnsureLayer(Database database, Transaction transaction, string name, short colorIndex)
    {
        var table = (LayerTable)transaction.GetObject(database.LayerTableId, OpenMode.ForRead);
        if (table.Has(name))
            return;
        table.UpgradeOpen();
        var record = new LayerTableRecord
        {
            Name = name,
            Color = CadColor.FromColorIndex(ColorMethod.ByAci, colorIndex)
        };
        table.Add(record);
        transaction.AddNewlyCreatedDBObject(record, true);
    }

    internal static int EraseLayerEntities(
        BlockTableRecord modelSpace,
        Transaction transaction,
        string layer)
    {
        var ids = modelSpace.Cast<ObjectId>().ToArray();
        var removed = 0;
        foreach (var id in ids)
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity ||
                !entity.Layer.Equals(layer, StringComparison.OrdinalIgnoreCase))
                continue;
            entity.UpgradeOpen();
            entity.Erase();
            removed++;
        }
        return removed;
    }

    internal static ObjectId AddCircle(
        BlockTableRecord modelSpace,
        Transaction transaction,
        string layer,
        short colorIndex,
        double x,
        double y,
        double radius,
        object metadata,
        string zoneHandle = "")
    {
        var circle = new Circle(new Point3d(x, y, 0), Vector3d.ZAxis, radius)
        {
            Layer = layer,
            Color = CadColor.FromColorIndex(ColorMethod.ByAci, colorIndex)
        };
        modelSpace.AppendEntity(circle);
        transaction.AddNewlyCreatedDBObject(circle, true);
        WriteMetadata(modelSpace.Database, transaction, circle, metadata, zoneHandle);
        return circle.ObjectId;
    }

    internal static void AddPlantingArea(
        BlockTableRecord modelSpace,
        Transaction transaction,
        string layer,
        short colorIndex,
        PlantingArea area,
        string zoneHandle = "")
    {
        foreach (var polygon in PolygonParts(area.Geometry))
        {
            var exterior = AddRingPolyline(
                modelSpace, transaction, layer, colorIndex, polygon.ExteriorRing, area, zoneHandle);
            var holes = new List<Polyline>();
            for (var ringIndex = 0; ringIndex < polygon.NumInteriorRings; ringIndex++)
                holes.Add(AddRingPolyline(
                    modelSpace, transaction, layer, colorIndex,
                    polygon.GetInteriorRingN(ringIndex), area, zoneHandle));
            var hatch = new Hatch
            {
                Layer = layer,
                Color = CadColor.FromColorIndex(ColorMethod.ByAci, colorIndex),
                Transparency = new Transparency(170)
            };
            modelSpace.AppendEntity(hatch);
            transaction.AddNewlyCreatedDBObject(hatch, true);
            hatch.SetHatchPattern(HatchPatternType.PreDefined, "SOLID");
            hatch.Associative = true;
            var boundaryIds = new ObjectIdCollection { exterior.ObjectId };
            hatch.AppendLoop(HatchLoopTypes.Outermost, boundaryIds);
            foreach (var hole in holes)
                hatch.AppendLoop(HatchLoopTypes.Default, new ObjectIdCollection { hole.ObjectId });
            hatch.EvaluateHatch(true);
            WriteMetadata(modelSpace.Database, transaction, hatch, area, zoneHandle);
        }
    }

    private static Polyline AddRingPolyline(
        BlockTableRecord modelSpace,
        Transaction transaction,
        string layer,
        short colorIndex,
        NetTopologySuite.Geometries.LineString ring,
        PlantingArea metadata,
        string zoneHandle)
    {
        var polyline = new Polyline(ring.NumPoints)
        {
            Layer = layer,
            Color = CadColor.FromColorIndex(ColorMethod.ByAci, colorIndex),
            Closed = true,
            ConstantWidth = 0.08
        };
        var coordinates = ring.Coordinates;
        for (var index = 0; index < coordinates.Length - 1; index++)
            polyline.AddVertexAt(index, new Point2d(coordinates[index].X, coordinates[index].Y), 0, 0, 0);
        modelSpace.AppendEntity(polyline);
        transaction.AddNewlyCreatedDBObject(polyline, true);
        WriteMetadata(modelSpace.Database, transaction, polyline, metadata, zoneHandle);
        return polyline;
    }

    internal static IReadOnlyList<NetTopologySuite.Geometries.Polygon> PolygonParts(
        NetTopologySuite.Geometries.Geometry geometry)
    {
        var result = new List<NetTopologySuite.Geometries.Polygon>();
        void Collect(NetTopologySuite.Geometries.Geometry item)
        {
            if (item is NetTopologySuite.Geometries.Polygon polygon)
            {
                result.Add(polygon);
                return;
            }
            if (item is not NetTopologySuite.Geometries.GeometryCollection)
                return;
            for (var index = 0; index < item.NumGeometries; index++)
                Collect(item.GetGeometryN(index));
        }
        Collect(geometry);
        return result;
    }

    private static void WriteMetadata(
        Database database,
        Transaction transaction,
        Entity entity,
        object metadata,
        string zoneHandle = "")
    {
        var table = (RegAppTable)transaction.GetObject(database.RegAppTableId, OpenMode.ForRead);
        if (!table.Has(MetadataApplication))
        {
            table.UpgradeOpen();
            var record = new RegAppTableRecord { Name = MetadataApplication };
            table.Add(record);
            transaction.AddNewlyCreatedDBObject(record, true);
        }
        var (id, type, species, status) = metadata switch
        {
            PlacementPoint item => (item.Id, item.PlantType, item.Species, item.Status),
            PlantingArea item => (item.Id, item.PlantType, item.Species, item.Status),
            PlacementValidation item =>
                (item.Placement.SourceId, item.Placement.PlantType, "", item.IsValid ? "accepted" : "rejected"),
            _ => ("", "", "", "")
        };
        entity.XData = new ResultBuffer(
            new TypedValue((int)DxfCode.ExtendedDataRegAppName, MetadataApplication),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, id ?? ""),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, type ?? ""),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, species ?? ""),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, status ?? ""),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, zoneHandle ?? ""));
    }

    internal static GreenAiMetadata? ReadMetadata(Entity entity)
    {
        using var pluginData = entity.GetXDataForApplication(MetadataApplication);
        using var batchData = pluginData is null
            ? entity.GetXDataForApplication(BatchMetadataApplication)
            : null;
        var data = pluginData ?? batchData;
        if (data is null)
            return null;
        var values = data.AsArray();
        if (values.Length < 5)
            return null;

        // The interactive plug-in stores plain values, while the batch DXF
        // exporter uses self-describing strings such as "id=T-0001".  Accept
        // both representations so GREENAI_INSPECT can open a passport for a
        // planting created by either workflow.
        static string ReadNamedValue(TypedValue[] source, string key)
        {
            var prefix = key + "=";
            for (var itemIndex = 1; itemIndex < source.Length; itemIndex++)
            {
                var raw = source[itemIndex].Value?.ToString() ?? "";
                if (raw.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    return raw[prefix.Length..];
            }
            return "";
        }

        static string ReadValue(TypedValue[] source, int index, string key)
        {
            var named = ReadNamedValue(source, key);
            if (!string.IsNullOrWhiteSpace(named))
                return named;
            if (index >= source.Length)
                return "";
            var raw = source[index].Value?.ToString() ?? "";
            var prefix = key + "=";
            return raw.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)
                ? raw[prefix.Length..]
                : raw;
        }

        var failures = new List<GreenAiFailureDetail>();
        for (var index = 1; index <= 32; index++)
        {
            var title = ReadNamedValue(values, $"title_{index}");
            var code = ReadNamedValue(values, $"code_{index}");
            var metric = ReadNamedValue(values, $"metric_{index}");
            var detail = ReadNamedValue(values, $"detail_{index}");
            var advice = ReadNamedValue(values, $"advice_{index}");
            var norm = ReadNamedValue(values, $"norm_{index}");
            if (string.IsNullOrWhiteSpace(title)
                && string.IsNullOrWhiteSpace(code)
                && string.IsNullOrWhiteSpace(metric)
                && string.IsNullOrWhiteSpace(detail)
                && string.IsNullOrWhiteSpace(advice)
                && string.IsNullOrWhiteSpace(norm))
                continue;
            failures.Add(new GreenAiFailureDetail(code, title, metric, detail, advice, norm));
        }

        return new GreenAiMetadata(
            ReadValue(values, 1, "id"),
            ReadValue(values, 2, "type"),
            ReadValue(values, 3, "species"),
            ReadValue(values, 4, "status"),
            ReadValue(values, 5, "zone"),
            ReadValue(values, 6, "failed"),
            ReadValue(values, 7, "reason"),
            failures,
            ReadNamedValue(values, "manual"));
    }

    internal sealed record GreenAiFailureDetail(
        string Code,
        string Title,
        string Metric,
        string Detail,
        string Advice,
        string NormReference);

    internal sealed record GreenAiMetadata(
        string Id,
        string PlantType,
        string Species,
        string Status,
        string ZoneHandle,
        string FailedChecks,
        string RejectionReason,
        IReadOnlyList<GreenAiFailureDetail> Failures,
        string ManualReviewChecks);

    internal static void SetLayerVisibility(string layer, bool visible)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        using (document.LockDocument())
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var table = (LayerTable)transaction.GetObject(document.Database.LayerTableId, OpenMode.ForRead);
            if (!table.Has(layer))
                return;
            var record = (LayerTableRecord)transaction.GetObject(table[layer], OpenMode.ForWrite);
            record.IsOff = !visible;
            transaction.Commit();
        }
        document.Editor.Regen();
    }

    internal static void ShowResultLayers(Document document, PluginConfig config)
    {
        var names = config.PlantingProfiles.Select(item => item.Layer)
            .Append(PreviewLayer)
            .Append(ReviewLayer)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        using (document.LockDocument())
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var table = (LayerTable)transaction.GetObject(document.Database.LayerTableId, OpenMode.ForRead);
            foreach (var name in names)
            {
                if (!table.Has(name))
                    continue;
                var record = (LayerTableRecord)transaction.GetObject(table[name], OpenMode.ForWrite);
                record.IsOff = false;
                if (record.IsFrozen)
                    record.IsFrozen = false;
            }
            transaction.Commit();
        }
        GreenAiPanel.ResultLayersShown();
        document.Editor.Regen();
    }
}
