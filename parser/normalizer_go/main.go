// normalizer_go converts extracted DXF primitives to traceable GeoJSONL
// features. It intentionally does not dissolve or polygonize the whole city
// drawing: those topological operations belong to a focused later step for
// work boundaries and buildings.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
)

type RawRecord struct {
	ObjectType      string          `json:"object_type"`
	SemanticType    string          `json:"semantic_type"`
	TargetGeometry  string          `json:"target_geometry"`
	Conversion      string          `json:"conversion"`
	SourceLayer     string          `json:"source_layer"`
	SourceLayerTail string          `json:"source_layer_tail"`
	DXFType         string          `json:"dxf_type"`
	Handle          interface{}     `json:"handle"`
	BlockPath       []string        `json:"block_path"`
	Geometry        json.RawMessage `json:"geometry"`
}

type GeoJSONGeometry struct {
	Type        string      `json:"type"`
	Coordinates interface{} `json:"coordinates"`
}

type Feature struct {
	Type       string          `json:"type"`
	ID         string          `json:"id"`
	Properties map[string]any  `json:"properties"`
	Geometry   GeoJSONGeometry `json:"geometry"`
}

var polygonTargets = map[string]bool{
	"work_boundary": true, "building": true, "sidewalk": true,
	"existing_tree_belt": true, "vegetation_boundary": true,
}

func objectMap(raw json.RawMessage) (map[string]json.RawMessage, error) {
	var result map[string]json.RawMessage
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func numberList(raw json.RawMessage) ([]float64, error) {
	var values []float64
	err := json.Unmarshal(raw, &values)
	return values, err
}

func numberLists(raw json.RawMessage) ([][]float64, error) {
	var values [][]float64
	err := json.Unmarshal(raw, &values)
	return values, err
}

func numberPathLists(raw json.RawMessage) ([][][]float64, error) {
	var values [][][]float64
	err := json.Unmarshal(raw, &values)
	return values, err
}

func requiredFloat(values map[string]json.RawMessage, key string) (float64, error) {
	var result float64
	raw, exists := values[key]
	if !exists {
		return 0, fmt.Errorf("missing %s", key)
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return 0, err
	}
	return result, nil
}

func requiredPoint(values map[string]json.RawMessage, key string) ([]float64, error) {
	raw, exists := values[key]
	if !exists {
		return nil, fmt.Errorf("missing %s", key)
	}
	point, err := numberList(raw)
	if err != nil || len(point) < 2 {
		return nil, fmt.Errorf("invalid %s", key)
	}
	return []float64{point[0], point[1]}, nil
}

func arcCoordinates(center []float64, radius, start, end, tolerance float64) [][]float64 {
	if radius <= 0 {
		return nil
	}
	if end < start {
		end += 360
	}
	sweep := (end - start) * math.Pi / 180
	steps := int(math.Ceil(math.Abs(sweep*radius) / tolerance))
	if steps < 8 {
		steps = 8
	}
	if steps > 720 {
		steps = 720
	}
	coordinates := make([][]float64, 0, steps+1)
	startRadians := start * math.Pi / 180
	for index := 0; index <= steps; index++ {
		angle := startRadians + sweep*float64(index)/float64(steps)
		coordinates = append(coordinates, []float64{center[0] + radius*math.Cos(angle), center[1] + radius*math.Sin(angle)})
	}
	return coordinates
}

func ellipseCoordinates(values map[string]json.RawMessage, tolerance float64) ([][]float64, []float64, error) {
	center, err := requiredPoint(values, "center")
	if err != nil {
		return nil, nil, err
	}
	axis, err := requiredPoint(values, "major_axis")
	if err != nil {
		return nil, nil, err
	}
	ratio, err := requiredFloat(values, "ratio")
	if err != nil || ratio <= 0 {
		return nil, nil, errors.New("invalid ratio")
	}
	start, err := requiredFloat(values, "start_param")
	if err != nil {
		return nil, nil, err
	}
	end, err := requiredFloat(values, "end_param")
	if err != nil {
		return nil, nil, err
	}
	if end < start {
		end += 2 * math.Pi
	}
	majorLength := math.Hypot(axis[0], axis[1])
	if majorLength == 0 {
		return nil, nil, errors.New("zero major axis")
	}
	steps := int(math.Ceil(2 * math.Pi * math.Max(majorLength, majorLength*ratio) / tolerance))
	if steps < 12 {
		steps = 12
	}
	if steps > 720 {
		steps = 720
	}
	coordinates := make([][]float64, 0, steps+1)
	for index := 0; index <= steps; index++ {
		angle := start + (end-start)*float64(index)/float64(steps)
		coordinates = append(coordinates, []float64{
			center[0] + axis[0]*math.Cos(angle) - axis[1]*ratio*math.Sin(angle),
			center[1] + axis[1]*math.Cos(angle) + axis[0]*ratio*math.Sin(angle),
		})
	}
	return coordinates, center, nil
}

func centroid(coordinates [][]float64) []float64 {
	if len(coordinates) == 0 {
		return nil
	}
	var x, y float64
	for _, coordinate := range coordinates {
		x += coordinate[0]
		y += coordinate[1]
	}
	return []float64{x / float64(len(coordinates)), y / float64(len(coordinates))}
}

// toGeometry converts a CAD primitive into a two-dimensional GeoJSON shape.
// Existing tree symbols are represented by a point, because their component
// arcs and circles are constraints around the same planting location.
func toGeometry(record RawRecord, tolerance float64) (GeoJSONGeometry, error) {
	values, err := objectMap(record.Geometry)
	if err != nil {
		return GeoJSONGeometry{}, err
	}
	var kind string
	if err := json.Unmarshal(values["kind"], &kind); err != nil {
		return GeoJSONGeometry{}, err
	}
	pointTarget := record.ObjectType == "existing_tree" || record.ObjectType == "utility_marker"
	polygonTarget := polygonTargets[record.ObjectType]

	if kind == "point" || kind == "insert_point" {
		location, err := requiredPoint(values, "location")
		return GeoJSONGeometry{Type: "Point", Coordinates: location}, err
	}
	if kind == "line" {
		start, startErr := requiredPoint(values, "start")
		end, endErr := requiredPoint(values, "end")
		if startErr != nil {
			return GeoJSONGeometry{}, startErr
		}
		if endErr != nil {
			return GeoJSONGeometry{}, endErr
		}
		if pointTarget {
			return GeoJSONGeometry{Type: "Point", Coordinates: []float64{(start[0] + end[0]) / 2, (start[1] + end[1]) / 2}}, nil
		}
		return GeoJSONGeometry{Type: "LineString", Coordinates: [][]float64{start, end}}, nil
	}
	if kind == "polyline" {
		rawPoints, exists := values["points"]
		if !exists {
			return GeoJSONGeometry{}, errors.New("missing points")
		}
		points, err := numberLists(rawPoints)
		if err != nil || len(points) < 2 {
			return GeoJSONGeometry{}, errors.New("invalid polyline")
		}
		coordinates := make([][]float64, 0, len(points)+1)
		for _, point := range points {
			coordinates = append(coordinates, []float64{point[0], point[1]})
		}
		var closed bool
		_ = json.Unmarshal(values["closed"], &closed)
		if closed && (coordinates[0][0] != coordinates[len(coordinates)-1][0] || coordinates[0][1] != coordinates[len(coordinates)-1][1]) {
			coordinates = append(coordinates, coordinates[0])
		}
		if pointTarget {
			return GeoJSONGeometry{Type: "Point", Coordinates: centroid(coordinates)}, nil
		}
		if polygonTarget && closed {
			return GeoJSONGeometry{Type: "Polygon", Coordinates: [][][]float64{coordinates}}, nil
		}
		return GeoJSONGeometry{Type: "LineString", Coordinates: coordinates}, nil
	}
	if kind == "circle" || kind == "arc" {
		center, err := requiredPoint(values, "center")
		if err != nil {
			return GeoJSONGeometry{}, err
		}
		radius, err := requiredFloat(values, "radius")
		if err != nil {
			return GeoJSONGeometry{}, err
		}
		if pointTarget {
			return GeoJSONGeometry{Type: "Point", Coordinates: center}, nil
		}
		start, end := 0.0, 360.0
		if kind == "arc" {
			start, err = requiredFloat(values, "start_angle")
			if err != nil {
				return GeoJSONGeometry{}, err
			}
			end, err = requiredFloat(values, "end_angle")
			if err != nil {
				return GeoJSONGeometry{}, err
			}
		}
		coordinates := arcCoordinates(center, radius, start, end, tolerance)
		if polygonTarget && kind == "circle" {
			return GeoJSONGeometry{Type: "Polygon", Coordinates: [][][]float64{coordinates}}, nil
		}
		return GeoJSONGeometry{Type: "LineString", Coordinates: coordinates}, nil
	}
	if kind == "ellipse" {
		coordinates, center, err := ellipseCoordinates(values, tolerance)
		if err != nil {
			return GeoJSONGeometry{}, err
		}
		if pointTarget {
			return GeoJSONGeometry{Type: "Point", Coordinates: center}, nil
		}
		if polygonTarget && len(coordinates) > 1 && coordinates[0][0] == coordinates[len(coordinates)-1][0] && coordinates[0][1] == coordinates[len(coordinates)-1][1] {
			return GeoJSONGeometry{Type: "Polygon", Coordinates: [][][]float64{coordinates}}, nil
		}
		return GeoJSONGeometry{Type: "LineString", Coordinates: coordinates}, nil
	}
	if kind == "hatch" {
		rawPaths, exists := values["boundary_paths"]
		if !exists {
			return GeoJSONGeometry{}, errors.New("missing hatch boundary_paths")
		}
		paths, err := numberPathLists(rawPaths)
		if err != nil {
			return GeoJSONGeometry{}, err
		}
		rings := make([][][]float64, 0, len(paths))
		for _, path := range paths {
			if len(path) < 3 {
				continue
			}
			ring := make([][]float64, 0, len(path)+1)
			for _, point := range path {
				if len(point) >= 2 {
					ring = append(ring, []float64{point[0], point[1]})
				}
			}
			if len(ring) < 3 {
				continue
			}
			first, last := ring[0], ring[len(ring)-1]
			if first[0] != last[0] || first[1] != last[1] {
				ring = append(ring, first)
			}
			rings = append(rings, ring)
		}
		if len(rings) == 0 {
			return GeoJSONGeometry{}, errors.New("empty hatch boundary_paths")
		}
		return GeoJSONGeometry{Type: "Polygon", Coordinates: rings}, nil
	}
	return GeoJSONGeometry{}, fmt.Errorf("unsupported geometry kind: %s", kind)
}

func parseArgs(args []string) (input, output, report string, tolerance float64, err error) {
	output, report, tolerance = "normalized_primitives.geojsonl", "normalization_go_report.json", 0.1
	for index := 0; index < len(args); index++ {
		argument := args[index]
		if argument == "--output" || argument == "--report" || argument == "--curve-tolerance" {
			if index+1 == len(args) {
				return "", "", "", 0, fmt.Errorf("%s requires a value", argument)
			}
			index++
			value := args[index]
			switch argument {
			case "--output":
				output = value
			case "--report":
				report = value
			case "--curve-tolerance":
				tolerance, err = strconv.ParseFloat(value, 64)
				if err != nil {
					return "", "", "", 0, err
				}
			}
			continue
		}
		if strings.HasPrefix(argument, "-") {
			return "", "", "", 0, fmt.Errorf("unknown option: %s", argument)
		}
		if input != "" {
			return "", "", "", 0, errors.New("only one input JSONL is allowed")
		}
		input = argument
	}
	if input == "" || tolerance <= 0 {
		return "", "", "", 0, errors.New("input and positive curve tolerance are required")
	}
	return input, output, report, tolerance, nil
}

func main() {
	input, output, report, tolerance, err := parseArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "Usage: normalizer_go <extracted.jsonl> [--output features.geojsonl] [--report report.json] [--curve-tolerance 0.1]")
		os.Exit(2)
	}
	source, err := os.Open(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer source.Close()
	destination, err := os.Create(output)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer destination.Close()
	scanner := bufio.NewScanner(source)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	writer := bufio.NewWriter(destination)
	encoder := json.NewEncoder(writer)
	counts, skipped := map[string]int{}, map[string]int{}
	index := 0
	for scanner.Scan() {
		var record RawRecord
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			fmt.Fprintln(os.Stderr, "JSONL parse error:", err)
			os.Exit(1)
		}
		geometry, geometryErr := toGeometry(record, tolerance)
		if geometryErr != nil {
			skipped[record.ObjectType]++
			continue
		}
		index++
		counts[record.ObjectType]++
		feature := Feature{Type: "Feature", ID: fmt.Sprintf("%s:%06d", record.ObjectType, index), Properties: map[string]any{
			"object_type": record.ObjectType, "semantic_type": record.SemanticType, "target_geometry": record.TargetGeometry,
			"conversion": record.Conversion, "coordinate_reference": "local_dxf_coordinates", "source_layer": record.SourceLayer,
			"source_layer_tail": record.SourceLayerTail, "dxf_type": record.DXFType, "handle": record.Handle, "block_path": record.BlockPath,
		}, Geometry: geometry}
		if err := encoder.Encode(feature); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := writer.Flush(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	reportData, _ := json.MarshalIndent(map[string]any{"input": input, "output": output, "coordinate_reference": "local_dxf_coordinates", "curve_tolerance_in_dxf_units": tolerance, "written_features": counts, "skipped_without_geometry": skipped}, "", "  ")
	if err := os.WriteFile(report, append(reportData, '\n'), 0644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println("Read:", input)
	fmt.Println("Output:", output)
	fmt.Println("Report:", report)
	for objectType, count := range counts {
		fmt.Printf("  %s: %d features\n", objectType, count)
	}
	for objectType, count := range skipped {
		fmt.Printf("WARNING: %s: %d primitives without usable geometry\n", objectType, count)
	}
}
