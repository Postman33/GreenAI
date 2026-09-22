package main

import (
	"encoding/json"
	"math"
	"testing"
)

func TestArcCoordinatesWrapThroughZero(t *testing.T) {
	coordinates := arcCoordinates([]float64{0, 0}, 10, 350, 10, 0.2)
	if len(coordinates) < 9 {
		t.Fatalf("too few arc coordinates: %d", len(coordinates))
	}
	if math.Abs(coordinates[0][0]-10*math.Cos(350*math.Pi/180)) > 1e-6 {
		t.Fatalf("unexpected first point: %v", coordinates[0])
	}
	last := coordinates[len(coordinates)-1]
	if math.Abs(last[0]-10*math.Cos(10*math.Pi/180)) > 1e-6 {
		t.Fatalf("unexpected last point: %v", last)
	}
}

func TestToGeometryCreatesClosedWorkBoundary(t *testing.T) {
	record := RawRecord{
		ObjectType: "work_boundary",
		Geometry:   json.RawMessage(`{"kind":"polyline","closed":true,"points":[[0,0],[2,0],[2,2],[0,2]]}`),
	}
	geometry, err := toGeometry(record, 0.1)
	if err != nil {
		t.Fatal(err)
	}
	if geometry.Type != "Polygon" {
		t.Fatalf("unexpected geometry type: %s", geometry.Type)
	}
	ring := geometry.Coordinates.([][][]float64)[0]
	if len(ring) != 5 || ring[0][0] != ring[4][0] || ring[0][1] != ring[4][1] {
		t.Fatalf("polygon ring is not closed: %v", ring)
	}
}

func TestParseArgs(t *testing.T) {
	input, output, report, tolerance, err := parseArgs([]string{"input.jsonl", "--output", "out.jsonl", "--curve-tolerance", "0.25"})
	if err != nil {
		t.Fatal(err)
	}
	if input != "input.jsonl" || output != "out.jsonl" || report != "normalization_go_report.json" || tolerance != 0.25 {
		t.Fatalf("unexpected arguments: %q %q %q %v", input, output, report, tolerance)
	}
}
