using System.Globalization;
using System.Text.Json;
using NetTopologySuite.Geometries;
using NetTopologySuite.Operation.Union;

namespace GreenAI.Core;

public sealed class GeoJsonlGeometryReader
{
    private readonly GeometryFactory _factory = new(new PrecisionModel(), 0);

    public IReadOnlyDictionary<string, Geometry> ReadByObjectType(
        string path,
        ISet<string>? includedObjectTypes = null)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException("GeoJSONL file was not found", path);

        var grouped = new Dictionary<string, List<Geometry>>(StringComparer.OrdinalIgnoreCase);
        var lineNumber = 0;
        foreach (var line in File.ReadLines(path))
        {
            lineNumber++;
            if (string.IsNullOrWhiteSpace(line))
                continue;
            using var document = JsonDocument.Parse(line);
            var feature = document.RootElement;
            if (!feature.TryGetProperty("type", out var featureType) || featureType.GetString() != "Feature")
                throw new InvalidDataException($"{path}:{lineNumber}: expected GeoJSON Feature");
            if (!feature.TryGetProperty("properties", out var properties) ||
                !properties.TryGetProperty("object_type", out var objectTypeElement))
                throw new InvalidDataException($"{path}:{lineNumber}: object_type is missing");
            var objectType = objectTypeElement.GetString();
            if (string.IsNullOrWhiteSpace(objectType))
                continue;
            if (includedObjectTypes is not null && !includedObjectTypes.Contains(objectType))
                continue;
            if (!feature.TryGetProperty("geometry", out var geometryElement) || geometryElement.ValueKind == JsonValueKind.Null)
                continue;
            var geometry = ReadGeometry(geometryElement);
            if (geometry.IsEmpty)
                continue;
            if (!grouped.TryGetValue(objectType, out var items))
                grouped[objectType] = items = new List<Geometry>();
            items.Add(geometry);
        }

        return grouped.ToDictionary(
            item => item.Key,
            item => item.Value.Count == 1 ? item.Value[0] : UnaryUnionOp.Union(item.Value),
            StringComparer.OrdinalIgnoreCase);
    }

    private Geometry ReadGeometry(JsonElement element)
    {
        var type = element.GetProperty("type").GetString();
        return type switch
        {
            "Point" => _factory.CreatePoint(ReadCoordinate(element.GetProperty("coordinates"))),
            "MultiPoint" => _factory.CreateMultiPointFromCoords(ReadCoordinates(element.GetProperty("coordinates"))),
            "LineString" => _factory.CreateLineString(ReadCoordinates(element.GetProperty("coordinates"))),
            "MultiLineString" => _factory.CreateMultiLineString(
                element.GetProperty("coordinates").EnumerateArray()
                    .Select(item => _factory.CreateLineString(ReadCoordinates(item))).ToArray()),
            "Polygon" => ReadPolygon(element.GetProperty("coordinates")),
            "MultiPolygon" => _factory.CreateMultiPolygon(
                element.GetProperty("coordinates").EnumerateArray().Select(ReadPolygon).ToArray()),
            "GeometryCollection" => _factory.CreateGeometryCollection(
                element.GetProperty("geometries").EnumerateArray().Select(ReadGeometry).ToArray()),
            _ => throw new NotSupportedException($"Unsupported GeoJSON geometry: {type}")
        };
    }

    private Polygon ReadPolygon(JsonElement coordinates)
    {
        var rings = coordinates.EnumerateArray().Select(ReadRing).ToArray();
        if (rings.Length == 0)
            return _factory.CreatePolygon();
        return _factory.CreatePolygon(rings[0], rings.Skip(1).ToArray());
    }

    private LinearRing ReadRing(JsonElement element)
    {
        var coordinates = ReadCoordinates(element).ToList();
        if (coordinates.Count > 0 && !coordinates[0].Equals2D(coordinates[^1]))
            coordinates.Add(coordinates[0].Copy());
        return _factory.CreateLinearRing(coordinates.ToArray());
    }

    private static Coordinate[] ReadCoordinates(JsonElement element) =>
        element.EnumerateArray().Select(ReadCoordinate).ToArray();

    private static Coordinate ReadCoordinate(JsonElement element)
    {
        var values = element.EnumerateArray().Take(3).Select(value => value.GetDouble()).ToArray();
        if (values.Length < 2)
            throw new InvalidDataException("GeoJSON coordinate must contain X and Y");
        return values.Length >= 3
            ? new CoordinateZ(values[0], values[1], values[2])
            : new Coordinate(values[0], values[1]);
    }
}
