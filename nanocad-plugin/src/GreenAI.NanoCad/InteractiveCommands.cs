using System.Globalization;
using System.Text.Json;
using GreenAI.Core;
using HostMgd.EditorInput;
using NetTopologySuite;
using NetTopologySuite.Geometries;
using Teigha.DatabaseServices;
using Teigha.Runtime;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

public sealed partial class Commands
{
    private static readonly GeometryFactory GeometryFactory =
        NtsGeometryServices.Instance.CreateGeometryFactory(srid: 0);

    [CommandMethod("GREENAI_CONTEXT_VISUAL_PREP")]
    public void ContextVisualPrep()
    {
        if (Environment.GetEnvironmentVariable("GREENAI_CONTEXT_VISUAL_PREP") != "1")
            return;
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        ObjectId id;
        using (document.LockDocument())
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
            var polyline = new Polyline(4) { Closed = true };
            polyline.AddVertexAt(0, new Teigha.Geometry.Point2d(0, 0), 0, 0, 0);
            polyline.AddVertexAt(1, new Teigha.Geometry.Point2d(10, 0), 0, 0, 0);
            polyline.AddVertexAt(2, new Teigha.Geometry.Point2d(10, 10), 0, 0, 0);
            polyline.AddVertexAt(3, new Teigha.Geometry.Point2d(0, 10), 0, 0, 0);
            id = modelSpace.AppendEntity(polyline);
            transaction.AddNewlyCreatedDBObject(polyline, true);
            transaction.Commit();
        }
        document.Editor.SetImpliedSelection(new[] { id });
        var probePath = Environment.GetEnvironmentVariable("GREENAI_CONTEXT_PREP_FILE");
        if (!string.IsNullOrWhiteSpace(probePath))
        {
            var directory = Path.GetDirectoryName(Path.GetFullPath(probePath));
            if (!string.IsNullOrWhiteSpace(directory))
                Directory.CreateDirectory(directory);
            File.WriteAllText(probePath, JsonSerializer.Serialize(new
            {
                selected = true,
                objectId = id.ToString(),
                processId = Environment.ProcessId
            }, new JsonSerializerOptions { WriteIndented = true }));
        }
    }

    [CommandMethod("GREENAI_CONTEXT_TREE", CommandFlags.UsePickSet)]
    public void ContextTree() =>
        BuildContextPreview(new GreenAiContextRequest("tree", 5.0, 5000));

    [CommandMethod("GREENAI_CONTEXT_SHRUB", CommandFlags.UsePickSet)]
    public void ContextShrub() =>
        BuildContextPreview(new GreenAiContextRequest("shrub", 2.0, 5000));

    [CommandMethod("GREENAI_CONTEXT_MIXED", CommandFlags.UsePickSet)]
    public void ContextMixed() =>
        BuildContextPreview(new GreenAiContextRequest("mixed", 5.0, 5000));

    [CommandMethod("GREENAI_CONTEXT_GRASS", CommandFlags.UsePickSet)]
    public void ContextGrass() =>
        BuildContextPreview(new GreenAiContextRequest("herbaceous", 0.5, 5000));

    [CommandMethod("GREENAI_CONTEXT_PREVIEW", CommandFlags.UsePickSet)]
    public void ContextPreview()
    {
        var request = GreenAiContextRequest.Pending;
        GreenAiContextRequest.Pending = null;
        if (request is null)
        {
            GreenAiPanel.ShowDetails(
                "GreenAI",
                new[] { "Параметры контекстной посадки не найдены. Повторите выбор через ПКМ." },
                true);
            return;
        }
        BuildContextPreview(request);
    }

    [CommandMethod("GREENAI_CONTEXT_CUSTOM", CommandFlags.UsePickSet)]
    public void ContextCustom()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var objectId = GetContextObject(document.Editor);
            var allowAreaPlans = IsClosedContextPolyline(document.Database, objectId);
            using var dialog = new GreenAiPlantingDialog(allowAreaPlans);
            if (HostApplication.ShowModalDialog(dialog) != System.Windows.Forms.DialogResult.OK)
                return;
            BuildContextPreview(dialog.Request, objectId);
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось настроить посадку", new[] { error.Message }, true);
        }
    }

    private void BuildContextPreview(GreenAiContextRequest request, ObjectId? selectedId = null)
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var objectId = selectedId ?? GetContextObject(document.Editor);
            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var entity = transaction.GetObject(objectId, OpenMode.ForRead) as Entity
                    ?? throw new InvalidOperationException("Выбранный объект не удалось прочитать.");
                switch (entity)
                {
                    case Polyline polyline when polyline.Closed && polyline.NumberOfVertices >= 3:
                    {
                        var zone = PolylineToPolygon(polyline);
                        if (zone.IsEmpty || zone.Area <= 0)
                            throw new InvalidOperationException("Замкнутый контур не образует корректную площадь.");
                        GreenAiSession.SelectedZone = zone;
                        GreenAiSession.SelectedGuide = null;
                        GreenAiSession.SelectedZoneLayer = polyline.Layer;
                        GreenAiSession.SelectedZoneHandle = polyline.Handle.ToString();
                        GreenAiSession.SelectedGuideHandle = "—";
                        GreenAiSession.Pattern = "grid";
                        GreenAiPanel.ZoneSelected(polyline.Layer, polyline.Handle.ToString(), zone.Area);
                        break;
                    }
                    case Polyline polyline when polyline.NumberOfVertices >= 2:
                    {
                        EnsureLineCompatibleRequest(request);
                        var guide = PolylineToLineString(polyline);
                        SetContextGuide(entity, guide);
                        break;
                    }
                    case Teigha.DatabaseServices.Line line:
                    {
                        EnsureLineCompatibleRequest(request);
                        var guide = GeometryFactory.CreateLineString(new[]
                        {
                            new Coordinate(line.StartPoint.X, line.StartPoint.Y),
                            new Coordinate(line.EndPoint.X, line.EndPoint.Y)
                        });
                        SetContextGuide(entity, guide);
                        break;
                    }
                    default:
                        throw new InvalidOperationException(
                            "Для посадки выберите замкнутую полилинию, LINE или открытую POLYLINE.");
                }
                GreenAiSession.SelectedDocumentName = document.Name;
                transaction.Commit();
            }

            GreenAiSession.PlantType = request.PlantType;
            GreenAiSession.SpacingM = request.SpacingM;
            GreenAiSession.MaxCount = request.MaxCount;
            Preview();
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось построить предпросмотр", new[] { error.Message }, true);
        }
    }

    private static void SetContextGuide(Entity entity, Geometry guide)
    {
        if (guide.IsEmpty || guide.Length <= 1e-8)
            throw new InvalidOperationException("Выбранная линия не имеет измеримой длины.");
        GreenAiSession.SelectedGuide = guide;
        GreenAiSession.SelectedGuideHandle = entity.Handle.ToString();
        GreenAiSession.SelectedZone = null;
        GreenAiSession.SelectedZoneHandle = "—";
        GreenAiSession.Pattern = "guide";
        GreenAiPanel.GuideSelected(entity.Layer, entity.Handle.ToString(), guide.Length);
    }

    private static void EnsureLineCompatibleRequest(GreenAiContextRequest request)
    {
        if (request.PlantType is "mixed" or "herbaceous")
            throw new InvalidOperationException(
                "Для линии выберите конкретно деревья или кустарники. Смешанный план и газон строятся внутри замкнутого контура.");
    }

    private static ObjectId GetContextObject(Editor editor)
    {
        var implied = editor.SelectImplied();
        if (implied.Status == PromptStatus.OK && implied.Value.GetObjectIds().Length > 0)
            return implied.Value.GetObjectIds()[0];
        var options = new PromptEntityOptions(
            "\nВыберите замкнутый контур или линию для GreenAI: ");
        options.SetRejectMessage("\nНужна замкнутая полилиния, LINE или POLYLINE.");
        options.AddAllowedClass(typeof(Polyline), true);
        options.AddAllowedClass(typeof(Teigha.DatabaseServices.Line), true);
        var selected = editor.GetEntity(options);
        if (selected.Status != PromptStatus.OK)
            throw new InvalidOperationException("Выбор объекта отменён.");
        return selected.ObjectId;
    }

    private static bool IsClosedContextPolyline(Database database, ObjectId objectId)
    {
        using var transaction = database.TransactionManager.StartTransaction();
        var result = transaction.GetObject(objectId, OpenMode.ForRead) is Polyline polyline &&
                     polyline.Closed && polyline.NumberOfVertices >= 3;
        transaction.Commit();
        return result;
    }

    [CommandMethod("GREENAI_SELECT_ZONE")]
    public void SelectZone()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var editor = document.Editor;
        try
        {
            var options = new PromptEntityOptions(
                "\nВыберите замкнутую полилинию, ограничивающую зону посадки: ");
            options.SetRejectMessage("\nНужна замкнутая полилиния.");
            options.AddAllowedClass(typeof(Polyline), true);
            var result = editor.GetEntity(options);
            if (result.Status != PromptStatus.OK)
            {
                GreenAiPanel.SetStatus("Выбор зоны отменён.");
                return;
            }

            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            using var transaction = document.Database.TransactionManager.StartTransaction();
            var polyline = (Polyline)transaction.GetObject(result.ObjectId, OpenMode.ForRead);
            if (!polyline.Closed || polyline.NumberOfVertices < 3)
                throw new InvalidOperationException("Выбранная полилиния должна быть замкнута и иметь минимум три вершины.");
            var zone = PolylineToPolygon(polyline);
            if (zone.IsEmpty || zone.Area <= 0)
                throw new InvalidOperationException("Из выбранного контура не удалось получить площадь.");
            GreenAiSession.SelectedZone = zone;
            GreenAiSession.SelectedGuide = null;
            GreenAiSession.SelectedZoneLayer = polyline.Layer;
            GreenAiSession.SelectedZoneHandle = polyline.Handle.ToString();
            GreenAiSession.SelectedGuideHandle = "—";
            GreenAiSession.SelectedDocumentName = document.Name;
            GreenAiPanel.ZoneSelected(polyline.Layer, polyline.Handle.ToString(), zone.Area);
            GreenAiPanel.SetStatus("Зона выбрана. Настройте растение и схему посадки.");
            transaction.Commit();
        }
        catch (System.Exception error)
        {
            GreenAiPanel.SetStatus("Не удалось выбрать зону.");
            GreenAiPanel.ShowDetails("Ошибка выбора зоны", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_SELECT_GUIDE")]
    public void SelectGuide()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var editor = document.Editor;
        try
        {
            var options = new PromptEntityOptions(
                "\nВыберите линию или полилинию — ось ряда посадок: ");
            options.SetRejectMessage("\nНужна LINE или POLYLINE.");
            options.AddAllowedClass(typeof(Teigha.DatabaseServices.Line), true);
            options.AddAllowedClass(typeof(Polyline), true);
            var result = editor.GetEntity(options);
            if (result.Status != PromptStatus.OK)
            {
                GreenAiPanel.SetStatus("Выбор линии посадки отменён.");
                return;
            }

            GreenAiSession.ResetPreview();
            ClearPreviewGraphics();
            using var transaction = document.Database.TransactionManager.StartTransaction();
            var entity = (Entity)transaction.GetObject(result.ObjectId, OpenMode.ForRead);
            Geometry guide = entity switch
            {
                Teigha.DatabaseServices.Line line => GeometryFactory.CreateLineString(new[]
                {
                    new Coordinate(line.StartPoint.X, line.StartPoint.Y),
                    new Coordinate(line.EndPoint.X, line.EndPoint.Y)
                }),
                Polyline polyline when polyline.NumberOfVertices >= 2 => PolylineToLineString(polyline),
                _ => throw new InvalidOperationException("Выбранный объект не является линией посадки.")
            };
            if (guide.IsEmpty || guide.Length <= 1e-8)
                throw new InvalidOperationException("У выбранной линии отсутствует измеримая длина.");

            GreenAiSession.SelectedGuide = guide;
            GreenAiSession.SelectedGuideHandle = entity.Handle.ToString();
            GreenAiSession.SelectedZone = null;
            GreenAiSession.SelectedZoneHandle = "—";
            GreenAiSession.SelectedDocumentName = document.Name;
            GreenAiPanel.GuideSelected(entity.Layer, entity.Handle.ToString(), guide.Length);
            GreenAiPanel.SetStatus("Линия посадки выбрана. Задайте растение и шаг, затем нажмите «Предпросмотр».");
            transaction.Commit();
        }
        catch (System.Exception error)
        {
            GreenAiPanel.SetStatus("Не удалось выбрать линию посадки.");
            GreenAiPanel.ShowDetails("Ошибка выбора линии", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_PREVIEW")]
    public void Preview()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            if (!string.IsNullOrWhiteSpace(GreenAiSession.SelectedDocumentName) &&
                !GreenAiSession.SelectedDocumentName.Equals(document.Name, StringComparison.OrdinalIgnoreCase))
            {
                GreenAiSession.ResetScope();
                GreenAiPanel.ScopeUnavailable();
                throw new InvalidOperationException(
                    "Рабочая зона была выбрана в другом чертеже. Выберите область или линию заново.");
            }
            // The selected CAD geometry is authoritative.  The palette combo
            // can lag behind an asynchronous GetEntity command, so a valid
            // guide must never be mistaken for a missing polygonal zone.
            var alongGuide = GreenAiSession.SelectedGuide is not null &&
                             (GreenAiSession.Pattern.Equals("guide", StringComparison.OrdinalIgnoreCase) ||
                              GreenAiSession.SelectedZone is null);
            if (!alongGuide && GreenAiSession.SelectedZone is null)
                throw new InvalidOperationException(
                    "Сначала выберите замкнутую рабочую зону либо линию посадки кнопками в шаге 1.");
            if (GreenAiSession.Pattern == "manual")
                throw new InvalidOperationException("Для ручной схемы используйте кнопку «Указать точку вручную».");

            var (config, configPath, dataDirectory) = LoadContext(document.Name);
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var isMixed = GreenAiSession.PlantType.Equals("mixed", StringComparison.OrdinalIgnoreCase);
            if (alongGuide && isMixed)
                throw new InvalidOperationException("Для ряда вдоль линии выберите конкретное растение: дерево или кустарник.");
            PlantingProfile? profile = isMixed ? null : config.PlantingProfiles.First(item =>
                item.PlantType.Equals(GreenAiSession.PlantType, StringComparison.OrdinalIgnoreCase));
            var allExisting = ReadPlacements(document, config);
            var replacementScope = alongGuide
                ? GreenAiSession.SelectedGuide!
                : GreenAiSession.SelectedZone!;
            var existing = ExcludePlacementsBeingReplaced(
                allExisting, config, replacementScope, alongGuide, profile);
            var replacedExistingCount = allExisting.Count - existing.Count;
            IReadOnlyList<PlacementPoint> preview;
            IReadOnlyList<PlantingArea> previewAreas;
            var previewSourceHandle = alongGuide
                ? GreenAiSession.SelectedGuideHandle
                : GreenAiSession.SelectedZoneHandle;
            var zoneHandleForMetadata = alongGuide ? string.Empty : previewSourceHandle;
            if (alongGuide)
            {
                preview = _engine.GenerateAlongGuide(
                    model,
                    GreenAiSession.PlantType,
                    GreenAiSession.SelectedGuide!,
                    GreenAiSession.SpacingM,
                    GreenAiSession.MaxCount,
                    existing);
                previewAreas = Array.Empty<PlantingArea>();
            }
            else if (isMixed)
            {
                var generated = new List<PlacementPoint>();
                foreach (var pointProfile in config.PlantingProfiles
                             .Where(item => item.GeometryKind == "point")
                             .OrderByDescending(item => item.FootprintRadiusM))
                {
                    var occupied = existing.Concat(generated.Select((item, index) =>
                        new ExistingPlacement(item.PlantType, item.X, item.Y, $"mixed-{index}"))).ToArray();
                    generated.AddRange(_engine.GenerateInArea(
                        model, pointProfile.PlantType, GreenAiSession.SelectedZone!,
                        GreenAiSession.Pattern, pointProfile.SpacingM,
                        GreenAiSession.MaxCount, occupied));
                }
                preview = generated;
                previewAreas = _engine.GenerateCoverageAreas(
                    model,
                    GreenAiSession.SelectedZone!,
                    generated,
                    GreenAiSession.Scenario);
            }
            else
            {
                preview = profile!.GeometryKind == "point"
                    ? _engine.GenerateInArea(
                        model, GreenAiSession.PlantType, GreenAiSession.SelectedZone!,
                        GreenAiSession.Pattern, GreenAiSession.SpacingM,
                        GreenAiSession.MaxCount, existing)
                    : Array.Empty<PlacementPoint>();
                previewAreas = profile.GeometryKind == "area"
                    ? _engine.GenerateCoverageAreas(model, GreenAiSession.SelectedZone!)
                        .Where(item => item.PlantType.Equals(
                            GreenAiSession.PlantType, StringComparison.OrdinalIgnoreCase)).ToArray()
                    : Array.Empty<PlantingArea>();
            }
            GreenAiSession.Preview = preview;
            GreenAiSession.PreviewAreas = previewAreas;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                EnsureLayer(document.Database, transaction, PreviewLayer, 4);
                EraseLayerEntities(modelSpace, transaction, PreviewLayer);
                foreach (var placement in preview)
                {
                    var placementProfile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                        placement.PlantType, StringComparison.OrdinalIgnoreCase));
                    AddCircle(
                        modelSpace,
                        transaction,
                        PreviewLayer,
                        4,
                        placement.X,
                        placement.Y,
                        placementProfile.SymbolRadiusM * config.DxfUnitsPerMeter,
                        placement,
                        zoneHandleForMetadata);
                }
                foreach (var area in previewAreas)
                    AddPlantingArea(
                        modelSpace, transaction, PreviewLayer, 4, area,
                        zoneHandleForMetadata);
                transaction.Commit();
            }
            GreenAiSession.PreviewContext = new GreenAiSession.PreviewContract(
                alongGuide ? "guide" : "zone",
                previewSourceHandle,
                (alongGuide ? GreenAiSession.SelectedGuide! : GreenAiSession.SelectedZone!).AsText(),
                document.Name,
                GreenAiSession.PlantType,
                GreenAiSession.Pattern,
                GreenAiSession.SpacingM,
                GreenAiSession.MaxCount,
                DateTimeOffset.Now);
            document.Editor.Regen();
            var previewCoordinates = preview.Select(item => new Coordinate(item.X, item.Y))
                .Concat(previewAreas.SelectMany(item => item.Geometry.Coordinates));
            ZoomToCoordinates(document.Editor, previewCoordinates);
            var unitArea = config.DxfUnitsPerMeter * config.DxfUnitsPerMeter;
            if (alongGuide)
                GreenAiPanel.GuidePreviewReady(
                    preview.Count(item => item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase)),
                    preview.Count(item => item.PlantType.Equals("shrub", StringComparison.OrdinalIgnoreCase)),
                    GreenAiSession.SelectedGuide!.Length / config.DxfUnitsPerMeter,
                    GreenAiSession.SpacingM);
            else
                GreenAiPanel.PreviewReady(
                    preview.Count(item => item.PlantType.Equals("tree", StringComparison.OrdinalIgnoreCase)),
                    preview.Count(item => item.PlantType.Equals("shrub", StringComparison.OrdinalIgnoreCase)),
                    previewAreas.Sum(item => item.Geometry.Area) / unitArea,
                    GreenAiSession.SelectedZone!.Area / unitArea);
            GreenAiPanel.Log(
                previewAreas.Count > 0 && preview.Count == 0
                    ? $"Предпросмотр покрытия: {previewAreas.Sum(item => item.Geometry.Area) / (config.DxfUnitsPerMeter * config.DxfUnitsPerMeter):F1} м²."
                    : $"Предпросмотр: {preview.Count} посадок, покрытие " +
                      $"{previewAreas.Sum(item => item.Geometry.Area) / (config.DxfUnitsPerMeter * config.DxfUnitsPerMeter):F1} м², схема " +
                      $"{(alongGuide ? "вдоль проектной линии" : GreenAiSession.Pattern == "boundary" ? "вдоль границы" : "плотная сетка")}, " +
                      $"с нормативным шагом.");
            if (preview.Count == 0 && !previewAreas.Any())
            {
                var details = BuildEmptyPreviewDetails(
                    model,
                    GreenAiSession.SelectedZone,
                    GreenAiSession.SelectedGuide,
                    GreenAiSession.PlantType,
                    replacedExistingCount,
                    config.DxfUnitsPerMeter);
                GreenAiPanel.ShowDetails(
                    "Посадка невозможна в выбранной области",
                    details,
                    false);
            }
        }
        catch (System.Exception error)
        {
            GreenAiSession.ResetPreview();
            GreenAiPanel.PreviewUnavailable();
            GreenAiPanel.SetStatus("Предпросмотр не построен.");
            GreenAiPanel.ShowDetails("Ошибка предпросмотра", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_APPLY")]
    public void ApplyPreview()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            if (GreenAiSession.Preview.Count == 0 && GreenAiSession.PreviewAreas.Count == 0)
                throw new InvalidOperationException("Сначала постройте непустой предпросмотр.");
            var previewContext = GreenAiSession.PreviewContext
                ?? throw new InvalidOperationException(
                    "Параметры изменились после расчёта. Постройте предпросмотр заново.");
            if (!previewContext.DocumentName.Equals(document.Name, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Предпросмотр построен для другого чертежа. Выберите область и выполните расчёт заново.");
            var (config, _, _) = LoadContext(document.Name);
            var replacedCount = 0;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                var currentScope = previewContext.ScopeKind == "zone"
                    ? ResolveZoneGeometry(document.Database, transaction, previewContext.SourceHandle)
                    : ResolveGuideGeometry(document.Database, transaction, previewContext.SourceHandle);
                if (currentScope is null ||
                    !currentScope.AsText().Equals(
                        previewContext.SourceGeometryWkt, StringComparison.Ordinal))
                    throw new InvalidOperationException(
                        "Исходная зона или линия удалена либо изменена. Выберите её заново и обновите предпросмотр.");
                replacedCount = ErasePlacementsBeingReplaced(
                    modelSpace,
                    transaction,
                    config,
                    currentScope,
                    previewContext.ScopeKind.Equals("guide", StringComparison.OrdinalIgnoreCase),
                    previewContext.PlantType,
                    previewContext.SourceHandle);
                foreach (var placement in GreenAiSession.Preview)
                {
                    var profile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                        placement.PlantType, StringComparison.OrdinalIgnoreCase));
                    EnsureLayer(document.Database, transaction, profile.Layer, profile.ColorIndex);
                    AddCircle(
                        modelSpace,
                        transaction,
                        profile.Layer,
                        profile.ColorIndex,
                        placement.X,
                        placement.Y,
                        profile.SymbolRadiusM * config.DxfUnitsPerMeter,
                        placement,
                        previewContext.ScopeKind == "zone" ? previewContext.SourceHandle : string.Empty);
                }
                foreach (var area in GreenAiSession.PreviewAreas)
                {
                    var profile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                        area.PlantType, StringComparison.OrdinalIgnoreCase));
                    EnsureLayer(document.Database, transaction, profile.Layer, profile.ColorIndex);
                    AddPlantingArea(
                        modelSpace, transaction, profile.Layer, profile.ColorIndex, area,
                        previewContext.ScopeKind == "zone" ? previewContext.SourceHandle : string.Empty);
                }
                EraseLayerEntities(modelSpace, transaction, PreviewLayer);
                transaction.Commit();
            }
            var lastCoordinates = GreenAiSession.Preview
                .Select(item => new Coordinate(item.X, item.Y))
                .Concat(GreenAiSession.PreviewAreas.SelectMany(item => item.Geometry.Coordinates))
                .Select(item => item.Copy())
                .ToArray();
            var count = GreenAiSession.Preview.Count;
            var areaSquare = GreenAiSession.PreviewAreas.Sum(item => item.Geometry.Area) /
                             (config.DxfUnitsPerMeter * config.DxfUnitsPerMeter);
            GreenAiSession.ResetPreview();
            GreenAiSession.LastResultCoordinates = lastCoordinates;
            GreenAiPanel.PreviewUnavailable();
            document.Editor.Regen();
            var replacementText = replacedCount > 0
                ? $" Предыдущих объектов в выбранной области заменено: {replacedCount}."
                : "";
            GreenAiPanel.SetStatus($"Добавлено: посадок {count}, покрытия {areaSquare:F1} м².{replacementText}");
            GreenAiPanel.Log($"Добавлено: посадок {count}, покрытия {areaSquare:F1} м².{replacementText}");
        }
        catch (System.Exception error)
        {
            GreenAiPanel.SetStatus("Не удалось добавить посадки.");
            GreenAiPanel.ShowDetails("Ошибка добавления", new[] { error.Message }, true);
        }
    }

    internal static void ClearPreviewGraphics()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        if (document is null)
            return;
        using (document.LockDocument())
        using (var transaction = document.Database.TransactionManager.StartTransaction())
        {
            var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
            EraseLayerEntities(modelSpace, transaction, PreviewLayer);
            transaction.Commit();
        }
        document.Editor.Regen();
    }

    [CommandMethod("GREENAI_ZOOM_RESULT")]
    public void ZoomResult()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var coordinates = GreenAiSession.Preview
                .Select(item => new Coordinate(item.X, item.Y))
                .Concat(GreenAiSession.PreviewAreas.SelectMany(item => item.Geometry.Coordinates))
                .ToList();
            if (coordinates.Count == 0)
                coordinates.AddRange(GreenAiSession.LastResultCoordinates.Select(item => item.Copy()));
            if (coordinates.Count == 0)
            {
                var (config, _, _) = LoadContext(document.Name);
                coordinates.AddRange(ReadResultCoordinates(document, config));
            }
            if (coordinates.Count == 0)
                throw new InvalidOperationException("На чертеже пока нет предпросмотра или добавленных посадок.");
            var (layerConfig, _, _) = LoadContext(document.Name);
            ShowResultLayers(document, layerConfig);
            ZoomToCoordinates(document.Editor, coordinates);
            GreenAiPanel.SetStatus($"Показано посадок: {coordinates.Count}.");
            GreenAiPanel.Log($"Камера переведена к {coordinates.Count} посадкам.");
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось показать результат", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_MANUAL")]
    public void ManualPlacement()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        var editor = document.Editor;
        try
        {
            var pointResult = editor.GetPoint(new PromptPointOptions("\nУкажите центр посадки: "));
            if (pointResult.Status != PromptStatus.OK)
            {
                GreenAiPanel.SetStatus("Ручная посадка отменена.");
                return;
            }
            var (config, configPath, dataDirectory) = LoadContext(document.Name);
            if (!string.IsNullOrWhiteSpace(GreenAiSession.SelectedDocumentName) &&
                !GreenAiSession.SelectedDocumentName.Equals(document.Name, StringComparison.OrdinalIgnoreCase))
            {
                GreenAiSession.ResetScope();
                GreenAiPanel.ScopeUnavailable();
            }
            if (GreenAiSession.PlantType.Equals("mixed", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException(
                    "Для ручной точки выберите конкретно дерево или кустарник.");
            var profile = config.PlantingProfiles.First(item => item.PlantType.Equals(
                GreenAiSession.PlantType, StringComparison.OrdinalIgnoreCase));
            if (profile.GeometryKind != "point")
                throw new InvalidOperationException(
                    "Травянистое покрытие задаётся выбранным контуром. Выберите зону и нажмите «Предпросмотр».");
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var existing = ReadPlacements(document, config).ToList();
            var candidate = new ExistingPlacement(
                GreenAiSession.PlantType,
                pointResult.Value.X,
                pointResult.Value.Y,
                $"{(GreenAiSession.PlantType == "tree" ? "T" : "S")}-M-{DateTimeOffset.Now:yyyyMMddHHmmssfff}",
                GreenAiSession.SelectedZone,
                GreenAiSession.SelectedZone is null
                    ? null
                    : $"контур {GreenAiSession.SelectedZoneHandle}");
            var validation = _engine.Validate(model, existing.Append(candidate).ToArray())[^1];
            var selectedZoneHandle = GreenAiSession.SelectedZone is null
                ? ""
                : GreenAiSession.SelectedZoneHandle;
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForWrite);
                if (validation.IsValid)
                {
                    EnsureLayer(document.Database, transaction, profile.Layer, profile.ColorIndex);
                    AddCircle(
                        modelSpace,
                        transaction,
                        profile.Layer,
                        profile.ColorIndex,
                        candidate.X,
                        candidate.Y,
                        profile.SymbolRadiusM * config.DxfUnitsPerMeter,
                        validation,
                        selectedZoneHandle);
                }
                else
                {
                    EnsureLayer(document.Database, transaction, ReviewLayer, 1);
                    AddCircle(
                        modelSpace,
                        transaction,
                        ReviewLayer,
                        1,
                        candidate.X,
                        candidate.Y,
                        1.5 * config.DxfUnitsPerMeter,
                        validation,
                        selectedZoneHandle);
                }
                transaction.Commit();
            }
            editor.Regen();

            if (!validation.IsValid)
            {
                GreenAiPanel.SetStatus("Посадка отклонена: точка находится в запрещённой зоне.");
                GreenAiPanel.ShowDetails("Здесь сажать нельзя", validation.Errors, true);
                return;
            }

            GreenAiSession.LastResultCoordinates = new[]
            {
                new Coordinate(candidate.X, candidate.Y)
            };
            GreenAiPanel.SetStatus("Ручная посадка добавлена.");
            GreenAiPanel.Log(
                $"Добавлена ручная посадка {profile.Species} в точке " +
                $"({candidate.X:F2}; {candidate.Y:F2}).");
            if (validation.ReviewReasons.Count > 0)
                GreenAiPanel.ShowDetails(
                    "Посадка добавлена с ручной проверкой",
                    validation.ReviewReasons,
                    false);
        }
        catch (System.Exception error)
        {
            GreenAiPanel.SetStatus("Ошибка ручной посадки.");
            GreenAiPanel.ShowDetails("Ошибка ручной посадки", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_INSPECT")]
    public void InspectPlacement()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var selected = SelectPlantEntity(document.Editor, "\nВыберите посадку GreenAI: ");
            if (selected.Status != PromptStatus.OK)
                return;
            var (config, configPath, dataDirectory) = LoadContext(document.Name);
            ExistingPlacement target;
            string id;
            string species;
            List<string>? areaPassport = null;
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                if (transaction.GetObject(selected.ObjectId, OpenMode.ForRead) is not Entity entity)
                    throw new InvalidOperationException("Не удалось прочитать выбранный объект.");
                var metadata = ReadMetadata(entity);
                if (entity.Layer.StartsWith("DEBUG_REJECTED_", StringComparison.OrdinalIgnoreCase))
                {
                    id = metadata?.Id ?? entity.Handle.ToString();
                    species = metadata?.Species ?? "";
                    transaction.Commit();
                    var rejectedDetails = new List<string>
                    {
                        $"Идентификатор кандидата: {id}",
                        $"Тип посадки: {metadata?.PlantType ?? "plant"}",
                        $"Растение: {species}",
                        "Результат: посадка в этой точке отклонена"
                    };
                    if (metadata?.Failures.Count > 0)
                    {
                        rejectedDetails.Add(
                            $"Найдено конкретных причин: {metadata.Failures.Count}.");
                        for (var failureIndex = 0; failureIndex < metadata.Failures.Count; failureIndex++)
                        {
                            var failure = metadata.Failures[failureIndex];
                            rejectedDetails.Add("");
                            rejectedDetails.Add($"{failureIndex + 1}. {failure.Title}");
                            if (!string.IsNullOrWhiteSpace(failure.Metric))
                                rejectedDetails.Add("   Расчёт: " + failure.Metric);
                            if (!string.IsNullOrWhiteSpace(failure.Detail))
                                rejectedDetails.Add("   Почему: " + failure.Detail);
                            if (!string.IsNullOrWhiteSpace(failure.Advice))
                                rejectedDetails.Add("   Что сделать: " + failure.Advice);
                            if (!string.IsNullOrWhiteSpace(failure.NormReference))
                                rejectedDetails.Add("   Основание: " + failure.NormReference);
                            if (!string.IsNullOrWhiteSpace(failure.Code))
                                rejectedDetails.Add("   Код проверки: " + failure.Code);
                        }
                    }
                    else
                    {
                        if (!string.IsNullOrWhiteSpace(metadata?.RejectionReason))
                            rejectedDetails.Add("Причина: " + metadata.RejectionReason);
                        if (!string.IsNullOrWhiteSpace(metadata?.FailedChecks))
                            rejectedDetails.Add(
                                "Технические коды проверок: " + metadata.FailedChecks);
                        rejectedDetails.Add(
                            "В этом DXF нет расширенной диагностики. Сформируйте его новой версией пайплайна.");
                    }
                    if (!string.IsNullOrWhiteSpace(metadata?.ManualReviewChecks))
                    {
                        rejectedDetails.Add("");
                        rejectedDetails.Add(
                            "Дополнительно нужна ручная проверка: " + metadata.ManualReviewChecks);
                        rejectedDetails.Add(
                            "Эти пункты требуют подтверждения исходных данных и сами по себе не являются причиной текущего отказа.");
                    }
                    GreenAiPanel.ShowPassport(
                        $"Почему нельзя сажать: {id}", rejectedDetails, true);
                    GreenAiPanel.SetStatus($"Открыто объяснение отклонённой точки {id}.");
                    return;
                }
                var profile = config.PlantingProfiles.FirstOrDefault(item =>
                    item.Layer.Equals(entity.Layer, StringComparison.OrdinalIgnoreCase))
                    ?? throw new InvalidOperationException("Выбранный объект не является посадкой GreenAI.");
                id = metadata?.Id ?? entity.Handle.ToString();
                species = metadata?.Species is { Length: > 0 } value ? value : profile.Species;
                if (profile.GeometryKind == "area")
                {
                    var squareUnits = entity switch
                    {
                        Polyline polyline => polyline.Area,
                        Hatch hatch => hatch.Area,
                        _ => 0.0
                    };
                    areaPassport = new List<string>
                    {
                        $"Идентификатор участка: {id}",
                        $"Тип: {profile.DisplayName}",
                        $"Покрытие: {species}",
                        $"Площадь выбранного фрагмента: {squareUnits / (config.DxfUnitsPerMeter * config.DxfUnitsPerMeter):F1} м²",
                        "Результат: участок находится внутри рассчитанной допустимой области",
                        "✓ Не пересекает дороги, тротуары, здания, твёрдые покрытия и подтверждённые инженерные объекты.",
                        $"✓ Выбор покрытия: {string.Join("; ", profile.SelectionReasons)}. Источник: {profile.CatalogReference}."
                    };
                    target = null!;
                }
                else if (entity is Circle circle)
                {
                    var requiredArea = ResolveZoneGeometry(
                        document.Database, transaction, metadata?.ZoneHandle);
                    target = new ExistingPlacement(
                        profile.PlantType,
                        circle.Center.X,
                        circle.Center.Y,
                        id,
                        requiredArea,
                        requiredArea is null ? null : $"контур {metadata!.ZoneHandle}");
                }
                else
                {
                    throw new InvalidOperationException("Для точечной посадки выберите окружность дерева или кустарника.");
                }
                transaction.Commit();
            }
            if (areaPassport is not null)
            {
                GreenAiPanel.ShowPassport($"Паспорт покрытия {id}", areaPassport, false);
                GreenAiPanel.SetStatus($"Открыт паспорт покрытия {id}.");
                return;
            }
            var model = PreparedModelCache.Get(_engine, config, configPath, dataDirectory);
            var placements = ReadPlacements(document, config);
            var validation = _engine.Validate(model, placements)
                .FirstOrDefault(item => item.Placement.SourceId == target.SourceId)
                ?? _engine.Validate(model, new[] { target })[0];
            var profileForTarget = config.PlantingProfiles.First(item => item.PlantType.Equals(
                target.PlantType, StringComparison.OrdinalIgnoreCase));
            var lines = new List<string>
            {
                $"Идентификатор: {id}",
                $"Тип: {profileForTarget.DisplayName}",
                $"Растение: {species}",
                $"Координаты: X={target.X:F2}; Y={target.Y:F2}",
                $"Радиус взрослого габарита: {profileForTarget.FootprintRadiusM:F2} м",
                validation.IsValid ? "Результат: посадка допустима" : "Результат: посадка недопустима"
            };
            lines.AddRange((validation.Checks ?? Array.Empty<RuleCheckResult>()).Select(item =>
                $"{(item.Status == "passed" ? "✓" : item.Status == "failed" ? "✗" : "!")} {item.Explanation}"));
            lines.AddRange(validation.ReviewReasons.Select(item => "! Ручная проверка: " + item));
            GreenAiPanel.ShowPassport($"Паспорт посадки {id}", lines, !validation.IsValid);
            GreenAiPanel.SetStatus($"Открыт паспорт посадки {id}.");
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось открыть паспорт", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_MOVE_PLANT")]
    public void MovePlacement()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var selected = SelectPlantEntity(document.Editor, "\nВыберите посадку для переноса: ");
            if (selected.Status != PromptStatus.OK)
                return;
            var destination = document.Editor.GetPoint(new PromptPointOptions("\nУкажите новое положение посадки: "));
            if (destination.Status != PromptStatus.OK)
                return;
            var (config, _, _) = LoadContext(document.Name);
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                if (transaction.GetObject(selected.ObjectId, OpenMode.ForWrite) is not Circle circle ||
                    config.PlantingProfiles.All(item => !item.Layer.Equals(circle.Layer, StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidOperationException("Выбранный объект не является деревом или кустарником GreenAI.");
                circle.Center = new Teigha.Geometry.Point3d(
                    destination.Value.X, destination.Value.Y, circle.Center.Z);
                transaction.Commit();
            }
            document.Editor.Regen();
            GreenAiPanel.Log("Посадка перенесена; выполняется повторная проверка.");
            Check();
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось перенести посадку", new[] { error.Message }, true);
        }
    }

    [CommandMethod("GREENAI_DELETE_PLANT")]
    public void DeletePlacement()
    {
        var document = HostApplication.DocumentManager.MdiActiveDocument;
        try
        {
            var selected = SelectPlantEntity(document.Editor, "\nВыберите посадку для удаления: ");
            if (selected.Status != PromptStatus.OK)
                return;
            var (config, _, _) = LoadContext(document.Name);
            using (document.LockDocument())
            using (var transaction = document.Database.TransactionManager.StartTransaction())
            {
                if (transaction.GetObject(selected.ObjectId, OpenMode.ForRead) is not Entity entity ||
                    config.PlantingProfiles.All(item => !item.Layer.Equals(entity.Layer, StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidOperationException("Выбранный объект не является посадкой GreenAI.");
                var metadata = ReadMetadata(entity);
                var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
                foreach (ObjectId id in modelSpace)
                {
                    if (transaction.GetObject(id, OpenMode.ForRead) is not Entity candidate ||
                        !candidate.Layer.Equals(entity.Layer, StringComparison.OrdinalIgnoreCase))
                        continue;
                    var candidateMetadata = ReadMetadata(candidate);
                    if (id != selected.ObjectId &&
                        (metadata is null || candidateMetadata?.Id != metadata.Id))
                        continue;
                    candidate.UpgradeOpen();
                    candidate.Erase();
                }
                transaction.Commit();
            }
            document.Editor.Regen();
            GreenAiPanel.SetStatus("Посадка удалена.");
            GreenAiPanel.Log("Выбранная посадка удалена.");
        }
        catch (System.Exception error)
        {
            GreenAiPanel.ShowDetails("Не удалось удалить посадку", new[] { error.Message }, true);
        }
    }

    private static PromptEntityResult SelectPlantEntity(Editor editor, string message)
    {
        var options = new PromptEntityOptions(message);
        options.SetRejectMessage("\nВыберите объект посадки GreenAI.");
        options.AddAllowedClass(typeof(Entity), false);
        return editor.GetEntity(options);
    }

    private static IReadOnlyList<ExistingPlacement> ExcludePlacementsBeingReplaced(
        IReadOnlyList<ExistingPlacement> source,
        PluginConfig config,
        Geometry scope,
        bool alongGuide,
        PlantingProfile? selectedProfile) =>
        source.Where(item => !ShouldReplacePlacement(
                item, config, scope, alongGuide, selectedProfile))
            .ToArray();

    private static bool ShouldReplacePlacement(
        ExistingPlacement placement,
        PluginConfig config,
        Geometry scope,
        bool alongGuide,
        PlantingProfile? selectedProfile)
    {
        var point = scope.Factory.CreatePoint(new Coordinate(placement.X, placement.Y));
        if (!alongGuide)
            return scope.Covers(point);
        if (selectedProfile is null)
            return false;
        var existingProfile = config.PlantingProfiles.FirstOrDefault(item =>
            item.PlantType.Equals(placement.PlantType, StringComparison.OrdinalIgnoreCase));
        var corridorM = selectedProfile.FootprintRadiusM +
                        (existingProfile?.FootprintRadiusM ?? 0.0);
        var corridor = Math.Max(corridorM * config.DxfUnitsPerMeter, 1e-6);
        return scope.Distance(point) <= corridor;
    }

    private static int ErasePlacementsBeingReplaced(
        BlockTableRecord modelSpace,
        Transaction transaction,
        PluginConfig config,
        Geometry scope,
        bool alongGuide,
        string selectedPlantType,
        string sourceHandle)
    {
        var pointProfiles = config.PlantingProfiles
            .Where(item => item.GeometryKind == "point")
            .ToDictionary(item => item.Layer, StringComparer.OrdinalIgnoreCase);
        var areaLayers = config.PlantingProfiles
            .Where(item => item.GeometryKind == "area")
            .Select(item => item.Layer)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        var selectedProfile = config.PlantingProfiles.FirstOrDefault(item =>
            item.PlantType.Equals(selectedPlantType, StringComparison.OrdinalIgnoreCase));
        var removed = 0;
        foreach (var id in modelSpace.Cast<ObjectId>().ToArray())
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity)
                continue;
            var erase = false;
            if (entity is Circle circle && pointProfiles.TryGetValue(circle.Layer, out var profile))
            {
                erase = ShouldReplacePlacement(
                    new ExistingPlacement(profile.PlantType, circle.Center.X, circle.Center.Y, ""),
                    config,
                    scope,
                    alongGuide,
                    selectedProfile);
            }
            else if (!alongGuide && areaLayers.Contains(entity.Layer))
            {
                var metadata = ReadMetadata(entity);
                erase = metadata is not null &&
                        metadata.ZoneHandle.Equals(sourceHandle, StringComparison.OrdinalIgnoreCase);
            }
            if (!erase)
                continue;
            entity.UpgradeOpen();
            entity.Erase();
            removed++;
        }
        return removed;
    }

    private static IReadOnlyList<string> BuildEmptyPreviewDetails(
        PreparedModel model,
        Geometry? selectedZone,
        Geometry? selectedGuide,
        string selectedPlantType,
        int replacedExistingCount,
        double unitsPerMeter)
    {
        var lines = new List<string>();
        if (replacedExistingCount > 0)
            lines.Add(
                $"Ранее созданные GreenAI-посадки внутри области не блокировали расчёт: " +
                $"найдено {replacedExistingCount}, при применении они были бы заменены.");

        var profiles = selectedPlantType.Equals("mixed", StringComparison.OrdinalIgnoreCase)
            ? model.PlantTypes.Values.ToArray()
            : model.PlantTypes.Values.Where(item => item.Profile.PlantType.Equals(
                selectedPlantType, StringComparison.OrdinalIgnoreCase)).ToArray();
        if (selectedGuide is not null)
        {
            lines.Add($"Длина выбранной линии: {selectedGuide.Length / unitsPerMeter:F1} м.");
            foreach (var prepared in profiles.Where(item => item.Profile.GeometryKind == "point"))
            {
                var allowedLength = prepared.AllowedArea.Intersection(selectedGuide).Length / unitsPerMeter;
                lines.Add(
                    $"{prepared.Profile.DisplayName}: допустимая часть линии — {allowedLength:F1} м; " +
                    $"минимальный габарит — {prepared.Profile.FootprintRadiusM * 2.0:F1} м.");
            }
        }
        else if (selectedZone is not null)
        {
            var unitArea = unitsPerMeter * unitsPerMeter;
            var baseIntersection = model.BaseArea.Intersection(selectedZone).Area / unitArea;
            lines.Add($"Площадь выбранного контура: {selectedZone.Area / unitArea:F1} м².");
            lines.Add($"Пересечение с подтверждённой областью озеленения: {baseIntersection:F1} м².");
            foreach (var prepared in profiles)
            {
                Geometry safeZone = prepared.Profile.GeometryKind == "point" &&
                                    prepared.Profile.FootprintRadiusM > 0
                    ? selectedZone.Buffer(-prepared.Profile.FootprintRadiusM * unitsPerMeter)
                    : selectedZone;
                var allowedArea = safeZone.IsEmpty
                    ? 0.0
                    : prepared.AllowedArea.Intersection(safeZone).Area / unitArea;
                lines.Add(
                    $"{prepared.Profile.DisplayName}: после границ, отступов и сетей остаётся " +
                    $"{allowedArea:F1} м².");
            }
        }

        lines.Add(
            "Попробуйте выбрать более крупный контур, кустарник вместо дерева или провести линию по центру свободной полосы.");
        return lines;
    }

    private static IReadOnlyList<ExistingPlacement> ReadPlacements(
        HostMgd.ApplicationServices.Document document,
        PluginConfig config)
    {
        var placements = new List<ExistingPlacement>();
        var zoneCache = new Dictionary<string, Geometry?>(StringComparer.OrdinalIgnoreCase);
        using var transaction = document.Database.TransactionManager.StartTransaction();
        var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
        foreach (ObjectId id in modelSpace)
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is not Circle circle)
                continue;
            var profile = config.PlantingProfiles.FirstOrDefault(item => item.Layer.Equals(
                circle.Layer, StringComparison.OrdinalIgnoreCase));
            if (profile is null)
                continue;
            var metadata = ReadMetadata(circle);
            var zoneHandle = metadata?.ZoneHandle ?? "";
            if (!zoneCache.TryGetValue(zoneHandle, out var requiredArea))
            {
                requiredArea = ResolveZoneGeometry(document.Database, transaction, zoneHandle);
                zoneCache[zoneHandle] = requiredArea;
            }
            placements.Add(new ExistingPlacement(
                profile.PlantType,
                circle.Center.X,
                circle.Center.Y,
                metadata?.Id is { Length: > 0 } stableId ? stableId : id.Handle.ToString(),
                requiredArea,
                requiredArea is null ? null : $"контур {zoneHandle}"));
        }
        transaction.Commit();
        return placements;
    }

    private static Geometry? ResolveZoneGeometry(
        Database database,
        Transaction transaction,
        string? zoneHandle)
    {
        if (string.IsNullOrWhiteSpace(zoneHandle) ||
            !long.TryParse(
                zoneHandle,
                NumberStyles.HexNumber,
                CultureInfo.InvariantCulture,
                out var handleValue))
            return null;
        try
        {
            var objectId = database.GetObjectId(false, new Handle(handleValue), 0);
            if (objectId.IsNull ||
                transaction.GetObject(objectId, OpenMode.ForRead) is not Polyline polyline ||
                !polyline.Closed || polyline.NumberOfVertices < 3)
                return null;
            return PolylineToPolygon(polyline);
        }
        catch
        {
            return null;
        }
    }

    private static Geometry? ResolveGuideGeometry(
        Database database,
        Transaction transaction,
        string? guideHandle)
    {
        if (string.IsNullOrWhiteSpace(guideHandle) ||
            !long.TryParse(
                guideHandle,
                NumberStyles.HexNumber,
                CultureInfo.InvariantCulture,
                out var handleValue))
            return null;
        try
        {
            var objectId = database.GetObjectId(false, new Handle(handleValue), 0);
            if (objectId.IsNull)
                return null;
            return transaction.GetObject(objectId, OpenMode.ForRead) switch
            {
                Teigha.DatabaseServices.Line line => GeometryFactory.CreateLineString(new[]
                {
                    new Coordinate(line.StartPoint.X, line.StartPoint.Y),
                    new Coordinate(line.EndPoint.X, line.EndPoint.Y)
                }),
                Polyline polyline when polyline.NumberOfVertices >= 2 => PolylineToLineString(polyline),
                _ => null
            };
        }
        catch
        {
            return null;
        }
    }

    private static IReadOnlyList<Coordinate> ReadResultCoordinates(
        HostMgd.ApplicationServices.Document document,
        PluginConfig config)
    {
        var coordinates = new List<Coordinate>();
        var layers = config.PlantingProfiles.Select(item => item.Layer)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);
        using var transaction = document.Database.TransactionManager.StartTransaction();
        var modelSpace = OpenModelSpace(document.Database, transaction, OpenMode.ForRead);
        foreach (ObjectId id in modelSpace)
        {
            if (transaction.GetObject(id, OpenMode.ForRead) is not Entity entity || !layers.Contains(entity.Layer))
                continue;
            if (entity is Circle circle)
                coordinates.Add(new Coordinate(circle.Center.X, circle.Center.Y));
            else if (entity is Polyline polyline)
                for (var index = 0; index < polyline.NumberOfVertices; index++)
                {
                    var point = polyline.GetPoint2dAt(index);
                    coordinates.Add(new Coordinate(point.X, point.Y));
                }
        }
        transaction.Commit();
        return coordinates;
    }

    private static Geometry PolylineToPolygon(Polyline polyline)
    {
        var coordinates = SamplePolylineCoordinates(polyline, close: true);
        Geometry geometry = GeometryFactory.CreatePolygon(coordinates.ToArray());
        if (!geometry.IsValid)
            geometry = geometry.Buffer(0);
        return geometry;
    }

    private static Geometry PolylineToLineString(Polyline polyline) =>
        GeometryFactory.CreateLineString(SamplePolylineCoordinates(polyline, polyline.Closed).ToArray());

    private static IReadOnlyList<Coordinate> SamplePolylineCoordinates(Polyline polyline, bool close)
    {
        var coordinates = new List<Coordinate>();
        for (var index = 0; index < polyline.NumberOfVertices; index++)
        {
            var point = polyline.GetPoint2dAt(index);
            coordinates.Add(new Coordinate(point.X, point.Y));
            var hasFollowingSegment = index < polyline.NumberOfVertices - 1 || polyline.Closed;
            if (!hasFollowingSegment)
                continue;
            var bulge = polyline.GetBulgeAt(index);
            if (Math.Abs(bulge) < 1e-9)
                continue;

            // DXF bulge encodes an included arc angle as 4*atan(bulge).
            // A fixed eight-segment approximation could cut metres inside a
            // large curved contour.  Sampling at no more than two degrees
            // keeps the CAD selection boundary close enough for the later
            // full-footprint containment test.
            var includedAngle = Math.Abs(4.0 * Math.Atan(bulge));
            var segmentCount = Math.Clamp(
                (int)Math.Ceiling(includedAngle / (Math.PI / 90.0)), 2, 360);
            for (var step = 1; step < segmentCount; step++)
            {
                var sampled = polyline.GetPointAtParameter(index + (double)step / segmentCount);
                coordinates.Add(new Coordinate(sampled.X, sampled.Y));
            }
        }
        if (close && coordinates.Count > 0 && !coordinates[0].Equals2D(coordinates[^1]))
            coordinates.Add(coordinates[0].Copy());
        return coordinates;
    }

    private static void ZoomToCoordinates(Editor editor, IEnumerable<Coordinate> source)
    {
        var coordinates = source.ToArray();
        if (coordinates.Length == 0)
            return;
        var minX = coordinates.Min(item => item.X);
        var maxX = coordinates.Max(item => item.X);
        var minY = coordinates.Min(item => item.Y);
        var maxY = coordinates.Max(item => item.Y);
        var width = Math.Max(maxX - minX, 10.0);
        var height = Math.Max(maxY - minY, 10.0);
        const double margin = 1.25;
        using var view = editor.GetCurrentView();
        view.CenterPoint = new Teigha.Geometry.Point2d((minX + maxX) / 2.0, (minY + maxY) / 2.0);
        view.Width = width * margin;
        view.Height = height * margin;
        editor.SetCurrentView(view);
    }
}
