using GreenAI.Core;
using NetTopologySuite.Geometries;

namespace GreenAI.NanoCad;

internal static class GreenAiSession
{
    public static Geometry? SelectedZone { get; set; }
    public static Geometry? SelectedGuide { get; set; }
    public static string SelectedZoneLayer { get; set; } = "—";
    public static string SelectedZoneHandle { get; set; } = "—";
    public static string SelectedGuideHandle { get; set; } = "—";
    public static string SelectedDocumentName { get; set; } = "";
    public static string PlantType { get; set; } = "tree";
    public static string Pattern { get; set; } = "grid";
    public static double SpacingM { get; set; } = 5.0;
    public static int MaxCount { get; set; } = 5000;
    public static string Scenario { get; set; } = "balanced";
    public static bool AutoValidateEdits { get; set; } = true;
    public static IReadOnlyList<PlacementPoint> Preview { get; set; } = Array.Empty<PlacementPoint>();
    public static IReadOnlyList<PlantingArea> PreviewAreas { get; set; } = Array.Empty<PlantingArea>();
    public static IReadOnlyList<Coordinate> LastResultCoordinates { get; set; } = Array.Empty<Coordinate>();
    public static PreviewContract? PreviewContext { get; set; }

    public static void ResetPreview()
    {
        Preview = Array.Empty<PlacementPoint>();
        PreviewAreas = Array.Empty<PlantingArea>();
        PreviewContext = null;
    }

    public static void ResetScope()
    {
        SelectedZone = null;
        SelectedGuide = null;
        SelectedZoneLayer = "—";
        SelectedZoneHandle = "—";
        SelectedGuideHandle = "—";
        SelectedDocumentName = "";
        ResetPreview();
    }

    internal sealed record PreviewContract(
        string ScopeKind,
        string SourceHandle,
        string SourceGeometryWkt,
        string DocumentName,
        string PlantType,
        string Pattern,
        double SpacingM,
        int MaxCount,
        DateTimeOffset CreatedAt);
}
