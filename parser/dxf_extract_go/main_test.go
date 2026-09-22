package main

import (
	"math"
	"testing"
)

func TestComposeTransformsPoint(t *testing.T) {
	parent := Matrix{A: 1, D: 1, TX: 10, TY: 20}
	child := Matrix{A: 0, B: -1, C: 1, D: 0, TX: 2, TY: 3}
	point := compose(parent, child).point(1, 0)
	if math.Abs(point[0]-12) > 1e-9 || math.Abs(point[1]-24) > 1e-9 {
		t.Fatalf("unexpected transformed point: %v", point)
	}
}

func TestLayerTailAndMatches(t *testing.T) {
	mapping := Mapping{
		Source:               "geobase_blocks",
		LayerTailIn:          []string{"Water"},
		DXFTypes:             []string{"LINE"},
		ExcludeLayerContains: []string{"project"},
	}
	entity := Entity{Type: "LINE", Layer: "xref$0$Water"}
	if layerTail(entity.Layer) != "Water" {
		t.Fatalf("unexpected layer tail: %q", layerTail(entity.Layer))
	}
	if !matches(mapping, entity, "geobase_blocks") {
		t.Fatal("expected mapping to match")
	}
	entity.Layer = "project$0$Water"
	if matches(mapping, entity, "geobase_blocks") {
		t.Fatal("excluded layer matched")
	}
}

func TestEntityGeometryTransformsLine(t *testing.T) {
	entity := Entity{
		Type:  "LINE",
		Pairs: []Pair{{Code: 10, Value: "1"}, {Code: 20, Value: "2"}, {Code: 11, Value: "3"}, {Code: 21, Value: "4"}},
	}
	geometry, ok := entityGeometry(entity, Matrix{A: 1, D: 1, TX: 10, TY: 20}).(map[string]interface{})
	if !ok {
		t.Fatalf("unexpected geometry type: %T", entityGeometry(entity, identity()))
	}
	start := geometry["start"].([]float64)
	end := geometry["end"].([]float64)
	if start[0] != 11 || start[1] != 22 || end[0] != 13 || end[1] != 24 {
		t.Fatalf("unexpected line coordinates: %v -> %v", start, end)
	}
}

func TestOptionalIntegerReadsTrueColor(t *testing.T) {
	entity := Entity{Pairs: []Pair{{Code: 420, Value: "65280"}}}
	if optionalInteger(entity, 420) != 65280 {
		t.Fatalf("unexpected true color: %v", optionalInteger(entity, 420))
	}
	if optionalInteger(entity, 62) != nil {
		t.Fatalf("missing optional integer should be nil")
	}
}
