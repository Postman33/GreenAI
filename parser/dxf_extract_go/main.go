// dxf_extract_go extracts the same configured CAD objects as src/loader.py.
// It is intentionally a narrow ASCII-DXF reader: it supports the primitive
// types used by the current configuration and recursively expands bound
// blocks, but does not aim to implement all of the DXF specification.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/encoding/charmap"
	"gopkg.in/yaml.v3"
)

type Config struct {
	GeobaseBlocks struct {
		NameRegex []string `yaml:"name_regex"`
		MaxDepth  int      `yaml:"max_depth"`
	} `yaml:"geobase_blocks"`
	LayerMapping map[string]Mapping `yaml:"layer_mapping"`
}

type Mapping struct {
	SemanticType         string   `yaml:"semantic_type"`
	Source               string   `yaml:"source"`
	Required             bool     `yaml:"required"`
	LayerTailIn          []string `yaml:"layer_tail_in"`
	LayerRegex           string   `yaml:"layer_regex"`
	ExcludeLayerContains []string `yaml:"exclude_layer_contains"`
	DXFTypes             []string `yaml:"dxf_types"`
	Geometry             string   `yaml:"geometry"`
	Conversion           string   `yaml:"conversion"`
}

type Pair struct {
	Code  int
	Value string
}

type Entity struct {
	Type   string
	Layer  string
	Handle string
	Pairs  []Pair
}

type Matrix struct {
	A, B, C, D float64
	TX, TY     float64
}

type Record struct {
	ObjectType      string      `json:"object_type"`
	SemanticType    string      `json:"semantic_type"`
	TargetGeometry  string      `json:"target_geometry"`
	Conversion      string      `json:"conversion"`
	SourceLayer     string      `json:"source_layer"`
	SourceLayerTail string      `json:"source_layer_tail"`
	DXFType         string      `json:"dxf_type"`
	Handle          interface{} `json:"handle"`
	BlockPath       []string    `json:"block_path"`
	Color           int         `json:"color"`
	TrueColor       interface{} `json:"true_color"`
	Linetype        string      `json:"linetype"`
	Lineweight      int         `json:"lineweight"`
	Geometry        interface{} `json:"geometry"`
}

func identity() Matrix { return Matrix{A: 1, D: 1} }

func compose(parent, child Matrix) Matrix {
	return Matrix{
		A:  parent.A*child.A + parent.B*child.C,
		B:  parent.A*child.B + parent.B*child.D,
		C:  parent.C*child.A + parent.D*child.C,
		D:  parent.C*child.B + parent.D*child.D,
		TX: parent.A*child.TX + parent.B*child.TY + parent.TX,
		TY: parent.C*child.TX + parent.D*child.TY + parent.TY,
	}
}

func (m Matrix) point(x, y float64) []float64 {
	return []float64{m.A*x + m.B*y + m.TX, m.C*x + m.D*y + m.TY}
}

func (m Matrix) vector(x, y float64) []float64 {
	return []float64{m.A*x + m.B*y, m.C*x + m.D*y}
}

func dxfPair(scanner *bufio.Scanner) (int, string, error) {
	if !scanner.Scan() {
		if scanner.Err() != nil {
			return 0, "", scanner.Err()
		}
		return 0, "", io.EOF
	}
	codeText := strings.TrimSpace(scanner.Text())
	if !scanner.Scan() {
		if scanner.Err() != nil {
			return 0, "", scanner.Err()
		}
		return 0, "", fmt.Errorf("incomplete DXF pair after group code %q", codeText)
	}
	code, err := strconv.Atoi(codeText)
	if err != nil {
		return 0, "", fmt.Errorf("invalid DXF group code %q: %w", codeText, err)
	}
	return code, strings.TrimSpace(scanner.Text()), nil
}

func decodeCP1251(value string) string {
	decoded, err := charmap.Windows1251.NewDecoder().String(value)
	if err != nil {
		return value
	}
	return decoded
}

func loadConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	var config Config
	if err := yaml.Unmarshal(data, &config); err != nil {
		return Config{}, err
	}
	if len(config.LayerMapping) == 0 {
		return Config{}, errors.New("config must contain layer_mapping")
	}
	if config.GeobaseBlocks.MaxDepth == 0 {
		config.GeobaseBlocks.MaxDepth = 12
	}
	return config, nil
}

func layerTail(layer string) string {
	parts := strings.Split(layer, "$0$")
	return parts[len(parts)-1]
}

func equalFoldAny(value string, candidates []string) bool {
	for _, candidate := range candidates {
		if strings.EqualFold(value, candidate) {
			return true
		}
	}
	return false
}

func matches(mapping Mapping, entity Entity, source string) bool {
	if (mapping.Source != source && mapping.Source != "both") || !equalFoldAny(entity.Type, mapping.DXFTypes) {
		return false
	}
	if len(mapping.LayerTailIn) > 0 && !equalFoldAny(layerTail(entity.Layer), mapping.LayerTailIn) {
		return false
	}
	if mapping.LayerRegex != "" {
		pattern, err := regexp.Compile("(?i)" + mapping.LayerRegex)
		if err != nil || !pattern.MatchString(entity.Layer) {
			return false
		}
	}
	lowerLayer := strings.ToLower(entity.Layer)
	for _, value := range mapping.ExcludeLayerContains {
		if strings.Contains(lowerLayer, strings.ToLower(value)) {
			return false
		}
	}
	return true
}

func hasGeobaseMatch(entity Entity, config Config) bool {
	for _, mapping := range config.LayerMapping {
		if matches(mapping, entity, "geobase_blocks") {
			return true
		}
	}
	return false
}

func appendEntity(blocks map[string][]Entity, model *[]Entity, section, blockName string, entity Entity, config Config) {
	if section == "BLOCKS" {
		if blockName != "" && (entity.Type == "INSERT" || hasGeobaseMatch(entity, config)) {
			blocks[blockName] = append(blocks[blockName], entity)
		}
		return
	}
	if section == "ENTITIES" {
		// Root geobase INSERTs do not match any modelspace layer rule, but they
		// are needed later to enter their nested geometry.
		if entity.Type == "INSERT" {
			*model = append(*model, entity)
			return
		}
		for _, mapping := range config.LayerMapping {
			if matches(mapping, entity, "modelspace") {
				*model = append(*model, entity)
				return
			}
		}
	}
}

// readDocument keeps only configured primitives and INSERT records. This is
// why its memory use stays bounded even for a large attached geobase.
func readDocument(path string, config Config) (map[string][]Entity, []Entity, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 8*1024*1024)
	blocks := make(map[string][]Entity)
	model := make([]Entity, 0, 1024)
	section, blockName := "", ""
	waitingSection, cp1251 := false, false
	var current *Entity

	finish := func() {
		if current == nil {
			return
		}
		if section == "BLOCKS" && current.Type == "BLOCK" {
			for _, pair := range current.Pairs {
				if pair.Code == 2 {
					blockName = pair.Value
					if _, exists := blocks[blockName]; !exists {
						blocks[blockName] = nil
					}
					break
				}
			}
		} else if section == "BLOCKS" && current.Type == "ENDBLK" {
			blockName = ""
		} else {
			appendEntity(blocks, &model, section, blockName, *current, config)
		}
		current = nil
	}

	for {
		code, rawValue, err := dxfPair(scanner)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, nil, err
		}
		value := rawValue
		// A few supplied drawings declare ANSI_1251 in the header while their
		// text is already UTF-8. Preserve valid UTF-8 and decode only raw bytes
		// that cannot be UTF-8.
		if cp1251 && !utf8.ValidString(rawValue) {
			value = decodeCP1251(rawValue)
		}

		if waitingSection && code == 2 {
			section = strings.ToUpper(value)
			waitingSection = false
			continue
		}
		if code == 9 && rawValue == "$DWGCODEPAGE" {
			// The following group code 3 contains an ASCII code-page name.
			continue
		}
		if code == 3 && strings.EqualFold(rawValue, "ANSI_1251") {
			cp1251 = true
			continue
		}

		if code == 0 {
			finish()
			typeName := strings.ToUpper(value)
			switch typeName {
			case "SECTION":
				waitingSection = true
			case "ENDSEC":
				section = ""
			default:
				if section == "BLOCKS" || section == "ENTITIES" {
					current = &Entity{Type: typeName, Pairs: make([]Pair, 0, 12)}
				}
			}
			continue
		}
		if current == nil {
			continue
		}
		current.Pairs = append(current.Pairs, Pair{Code: code, Value: value})
		if code == 8 {
			current.Layer = value
		}
		if code == 5 {
			current.Handle = value
		}
	}
	finish()
	return blocks, model, nil
}

func value(entity Entity, code int, fallback string) string {
	for _, pair := range entity.Pairs {
		if pair.Code == code {
			return pair.Value
		}
	}
	return fallback
}

func values(entity Entity, code int) []string {
	result := make([]string, 0)
	for _, pair := range entity.Pairs {
		if pair.Code == code {
			result = append(result, pair.Value)
		}
	}
	return result
}

func number(raw string, fallback float64) float64 {
	parsed, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return parsed
}

func integer(raw string, fallback int) int {
	parsed, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return parsed
}

func optionalInteger(entity Entity, code int) interface{} {
	raw := value(entity, code, "")
	if raw == "" {
		return nil
	}
	parsed, err := strconv.Atoi(raw)
	if err != nil {
		return nil
	}
	return parsed
}

func point(entity Entity, xCode, yCode int) (float64, float64) {
	return number(value(entity, xCode, "0"), 0), number(value(entity, yCode, "0"), 0)
}

func insertMatrix(entity Entity) Matrix {
	x, y := point(entity, 10, 20)
	sx := number(value(entity, 41, "1"), 1)
	sy := number(value(entity, 42, "1"), 1)
	angle := number(value(entity, 50, "0"), 0) * math.Pi / 180
	cos, sin := math.Cos(angle), math.Sin(angle)
	return Matrix{A: sx * cos, B: -sy * sin, C: sx * sin, D: sy * cos, TX: x, TY: y}
}

func pairValue(pairs []Pair, code int, fallback string) string {
	for _, pair := range pairs {
		if pair.Code == code {
			return pair.Value
		}
	}
	return fallback
}

func edgeCoordinates(pairs []Pair, edgeType int, matrix Matrix) [][]float64 {
	switch edgeType {
	case 1: // line
		start := matrix.point(number(pairValue(pairs, 10, "0"), 0), number(pairValue(pairs, 20, "0"), 0))
		end := matrix.point(number(pairValue(pairs, 11, "0"), 0), number(pairValue(pairs, 21, "0"), 0))
		return [][]float64{start, end}
	case 2: // circular arc
		center := []float64{number(pairValue(pairs, 10, "0"), 0), number(pairValue(pairs, 20, "0"), 0)}
		radius := number(pairValue(pairs, 40, "0"), 0)
		start := number(pairValue(pairs, 50, "0"), 0)
		end := number(pairValue(pairs, 51, "0"), 0)
		ccw := integer(pairValue(pairs, 73, "1"), 1) != 0
		if ccw && end < start {
			end += 360
		}
		if !ccw && end > start {
			end -= 360
		}
		steps := int(math.Ceil(math.Abs(end-start) / 10))
		if steps < 8 {
			steps = 8
		}
		coordinates := make([][]float64, 0, steps+1)
		for index := 0; index <= steps; index++ {
			angle := (start + (end-start)*float64(index)/float64(steps)) * math.Pi / 180
			coordinates = append(coordinates, matrix.point(center[0]+radius*math.Cos(angle), center[1]+radius*math.Sin(angle)))
		}
		return coordinates
	case 3: // elliptic arc
		centerX := number(pairValue(pairs, 10, "0"), 0)
		centerY := number(pairValue(pairs, 20, "0"), 0)
		majorX := number(pairValue(pairs, 11, "0"), 0)
		majorY := number(pairValue(pairs, 21, "0"), 0)
		ratio := number(pairValue(pairs, 40, "1"), 1)
		start := number(pairValue(pairs, 50, "0"), 0)
		end := number(pairValue(pairs, 51, "0"), 0)
		ccw := integer(pairValue(pairs, 73, "1"), 1) != 0
		if ccw && end < start {
			end += 360
		}
		if !ccw && end > start {
			end -= 360
		}
		steps := int(math.Ceil(math.Abs(end-start) / 10))
		if steps < 12 {
			steps = 12
		}
		coordinates := make([][]float64, 0, steps+1)
		for index := 0; index <= steps; index++ {
			angle := (start + (end-start)*float64(index)/float64(steps)) * math.Pi / 180
			x := centerX + majorX*math.Cos(angle) - majorY*ratio*math.Sin(angle)
			y := centerY + majorY*math.Cos(angle) + majorX*ratio*math.Sin(angle)
			coordinates = append(coordinates, matrix.point(x, y))
		}
		return coordinates
	case 4: // spline: preserve its control/fit frame as a line approximation
		xs, ys := []string{}, []string{}
		for _, pair := range pairs {
			if pair.Code == 10 {
				xs = append(xs, pair.Value)
			}
			if pair.Code == 20 {
				ys = append(ys, pair.Value)
			}
		}
		if len(xs) < 2 {
			xs, ys = []string{}, []string{}
			for _, pair := range pairs {
				if pair.Code == 11 {
					xs = append(xs, pair.Value)
				}
				if pair.Code == 21 {
					ys = append(ys, pair.Value)
				}
			}
		}
		coordinates := make([][]float64, 0, len(xs))
		for index := range xs {
			if index < len(ys) {
				coordinates = append(coordinates, matrix.point(number(xs[index], 0), number(ys[index], 0)))
			}
		}
		return coordinates
	}
	return nil
}

func appendCoordinates(target, addition [][]float64) [][]float64 {
	if len(addition) == 0 {
		return target
	}
	if len(target) > 0 && target[len(target)-1][0] == addition[0][0] && target[len(target)-1][1] == addition[0][1] {
		addition = addition[1:]
	}
	return append(target, addition...)
}

func hatchBoundaryPaths(entity Entity, matrix Matrix) [][][]float64 {
	pairs := entity.Pairs
	paths := make([][][]float64, 0)
	for index := 0; index < len(pairs); {
		if pairs[index].Code != 92 {
			index++
			continue
		}
		flags := integer(pairs[index].Value, 0)
		index++
		if flags&2 != 0 { // polyline boundary path
			closed, vertexCount := false, 0
			for index < len(pairs) && pairs[index].Code != 92 {
				if pairs[index].Code == 73 {
					closed = integer(pairs[index].Value, 0) != 0
				}
				if pairs[index].Code == 93 {
					vertexCount = integer(pairs[index].Value, 0)
					index++
					break
				}
				index++
			}
			coordinates := make([][]float64, 0, vertexCount+1)
			for len(coordinates) < vertexCount && index < len(pairs) && pairs[index].Code != 92 {
				if pairs[index].Code != 10 {
					index++
					continue
				}
				x := number(pairs[index].Value, 0)
				index++
				y := 0.0
				if index < len(pairs) && pairs[index].Code == 20 {
					y = number(pairs[index].Value, 0)
					index++
				}
				coordinates = append(coordinates, matrix.point(x, y))
			}
			if closed && len(coordinates) >= 3 {
				first, last := coordinates[0], coordinates[len(coordinates)-1]
				if first[0] != last[0] || first[1] != last[1] {
					coordinates = append(coordinates, first)
				}
			}
			if len(coordinates) >= 3 {
				paths = append(paths, coordinates)
			}
			continue
		}

		edgeCount := 0
		for index < len(pairs) && pairs[index].Code != 92 {
			if pairs[index].Code == 93 {
				edgeCount = integer(pairs[index].Value, 0)
				index++
				break
			}
			index++
		}
		coordinates := make([][]float64, 0)
		for edgeIndex := 0; edgeIndex < edgeCount && index < len(pairs); edgeIndex++ {
			for index < len(pairs) && pairs[index].Code != 72 && pairs[index].Code != 92 {
				index++
			}
			if index >= len(pairs) || pairs[index].Code == 92 {
				break
			}
			edgeType := integer(pairs[index].Value, 0)
			index++
			start := index
			for index < len(pairs) && pairs[index].Code != 72 && pairs[index].Code != 92 {
				index++
			}
			coordinates = appendCoordinates(coordinates, edgeCoordinates(pairs[start:index], edgeType, matrix))
		}
		if len(coordinates) >= 3 {
			first, last := coordinates[0], coordinates[len(coordinates)-1]
			if first[0] != last[0] || first[1] != last[1] {
				coordinates = append(coordinates, first)
			}
			paths = append(paths, coordinates)
		}
	}
	return paths
}

func entityGeometry(entity Entity, matrix Matrix) interface{} {
	switch entity.Type {
	case "LINE":
		x1, y1 := point(entity, 10, 20)
		x2, y2 := point(entity, 11, 21)
		return map[string]interface{}{"kind": "line", "start": matrix.point(x1, y1), "end": matrix.point(x2, y2)}
	case "POINT":
		x, y := point(entity, 10, 20)
		return map[string]interface{}{"kind": "point", "location": matrix.point(x, y)}
	case "CIRCLE":
		x, y := point(entity, 10, 20)
		// CAD xrefs normally use unit scale. The geometric mean keeps the
		// result usable when uniform scale is present.
		radius := number(value(entity, 40, "0"), 0) * math.Sqrt(math.Abs(matrix.A*matrix.D-matrix.B*matrix.C))
		return map[string]interface{}{"kind": "circle", "center": matrix.point(x, y), "radius": radius}
	case "ARC":
		x, y := point(entity, 10, 20)
		radius := number(value(entity, 40, "0"), 0) * math.Sqrt(math.Abs(matrix.A*matrix.D-matrix.B*matrix.C))
		return map[string]interface{}{"kind": "arc", "center": matrix.point(x, y), "radius": radius, "start_angle": number(value(entity, 50, "0"), 0), "end_angle": number(value(entity, 51, "0"), 0)}
	case "ELLIPSE":
		x, y := point(entity, 10, 20)
		ax, ay := point(entity, 11, 21)
		return map[string]interface{}{"kind": "ellipse", "center": matrix.point(x, y), "major_axis": matrix.vector(ax, ay), "ratio": number(value(entity, 40, "0"), 0), "start_param": number(value(entity, 41, "0"), 0), "end_param": number(value(entity, 42, "0"), 0)}
	case "LWPOLYLINE", "POLYLINE":
		xs, ys := values(entity, 10), values(entity, 20)
		starts, ends, bulges := values(entity, 40), values(entity, 41), values(entity, 42)
		points := make([][]float64, 0, len(xs))
		for i, rawX := range xs {
			y := 0.0
			if i < len(ys) {
				y = number(ys[i], 0)
			}
			xy := matrix.point(number(rawX, 0), y)
			start, end, bulge := 0.0, 0.0, 0.0
			if i < len(starts) {
				start = number(starts[i], 0)
			}
			if i < len(ends) {
				end = number(ends[i], 0)
			}
			if i < len(bulges) {
				bulge = number(bulges[i], 0)
			}
			points = append(points, []float64{xy[0], xy[1], start, end, bulge})
		}
		flags := integer(value(entity, 70, "0"), 0)
		return map[string]interface{}{"kind": "polyline", "closed": flags&1 != 0, "points": points}
	case "INSERT":
		x, y := point(entity, 10, 20)
		return map[string]interface{}{"kind": "insert_point", "location": matrix.point(x, y), "block_name": value(entity, 2, "")}
	case "HATCH":
		return map[string]interface{}{"kind": "hatch", "solid_fill": integer(value(entity, 70, "0"), 0), "pattern_name": value(entity, 2, ""), "boundary_paths": hatchBoundaryPaths(entity, matrix)}
	}
	return nil
}

type extractor struct {
	config      Config
	blocks      map[string][]Entity
	rootRegexes []*regexp.Regexp
	encoder     *json.Encoder
	counts      map[string]int
	dxfCounts   map[string]int
}

func (e *extractor) writeMatches(entity Entity, source string, path []string, matrix Matrix) error {
	if path == nil {
		// Keep the JSON contract of src/loader.py: a modelspace object has an
		// empty block path, not a null value.
		path = []string{}
	}
	for name, mapping := range e.config.LayerMapping {
		if !matches(mapping, entity, source) {
			continue
		}
		geometry := entityGeometry(entity, matrix)
		if geometry == nil {
			continue
		}
		handle := interface{}(entity.Handle)
		// ezdxf creates virtual entities while expanding a block and does not
		// retain their handles. Match that traceability contract exactly.
		if source == "geobase_blocks" {
			handle = nil
		}
		record := Record{
			ObjectType: name, SemanticType: mapping.SemanticType,
			TargetGeometry: mapping.Geometry, Conversion: mapping.Conversion,
			SourceLayer: entity.Layer, SourceLayerTail: layerTail(entity.Layer),
			DXFType: entity.Type, Handle: handle, BlockPath: path,
			Color:      integer(value(entity, 62, "256"), 256),
			TrueColor:  optionalInteger(entity, 420),
			Linetype:   value(entity, 6, "BYLAYER"),
			Lineweight: integer(value(entity, 370, "-1"), -1),
			Geometry:   geometry,
		}
		if err := e.encoder.Encode(record); err != nil {
			return err
		}
		e.counts[name]++
		e.dxfCounts[entity.Type]++
	}
	return nil
}

func (e *extractor) isRoot(name string) bool {
	for _, pattern := range e.rootRegexes {
		if pattern.MatchString(name) {
			return true
		}
	}
	return false
}

func (e *extractor) expand(insert Entity, path []string, matrix Matrix, depth int) error {
	if depth >= e.config.GeobaseBlocks.MaxDepth {
		return nil
	}
	name := value(insert, 2, "")
	children, exists := e.blocks[name]
	if !exists {
		fmt.Printf("WARNING: block definition was not found: %s\n", name)
		return nil
	}
	childMatrix := compose(matrix, insertMatrix(insert))
	for _, child := range children {
		if child.Type == "INSERT" {
			childName := value(child, 2, "")
			if err := e.expand(child, append(append([]string{}, path...), childName), childMatrix, depth+1); err != nil {
				return err
			}
			continue
		}
		if err := e.writeMatches(child, "geobase_blocks", path, childMatrix); err != nil {
			return err
		}
	}
	return nil
}

func sortedKeys(values map[string]int) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func parseArgs(arguments []string) (input, config, output string, err error) {
	config = filepath.Join("src", "core", "config.yaml")
	output = "extracted_objects_go.jsonl"
	for index := 0; index < len(arguments); index++ {
		argument := arguments[index]
		if argument == "--config" || argument == "--output" {
			if index+1 == len(arguments) {
				return "", "", "", fmt.Errorf("%s requires a value", argument)
			}
			index++
			if argument == "--config" {
				config = arguments[index]
			} else {
				output = arguments[index]
			}
			continue
		}
		if strings.HasPrefix(argument, "--config=") {
			config = strings.TrimPrefix(argument, "--config=")
			continue
		}
		if strings.HasPrefix(argument, "--output=") {
			output = strings.TrimPrefix(argument, "--output=")
			continue
		}
		if strings.HasPrefix(argument, "-") {
			return "", "", "", fmt.Errorf("unknown option: %s", argument)
		}
		if input != "" {
			return "", "", "", errors.New("only one input DXF is allowed")
		}
		input = argument
	}
	if input == "" {
		return "", "", "", errors.New("input DXF is required")
	}
	return input, config, output, nil
}

func main() {
	inputPath, configPath, outputPath, argumentError := parseArgs(os.Args[1:])
	if argumentError != nil {
		fmt.Fprintln(os.Stderr, "Usage: dxf_extract_go [--config config.yaml] [--output objects.jsonl] <input.dxf>")
		os.Exit(2)
	}
	config, err := loadConfig(configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Config error:", err)
		os.Exit(1)
	}
	blocks, model, err := readDocument(inputPath, config)
	if err != nil {
		fmt.Fprintln(os.Stderr, "DXF parse error:", err)
		os.Exit(1)
	}

	patterns := make([]*regexp.Regexp, 0, len(config.GeobaseBlocks.NameRegex))
	for _, source := range config.GeobaseBlocks.NameRegex {
		pattern, compileErr := regexp.Compile("(?i)" + source)
		if compileErr != nil {
			fmt.Fprintln(os.Stderr, "Config regex error:", compileErr)
			os.Exit(1)
		}
		patterns = append(patterns, pattern)
	}
	output, err := os.Create(outputPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Output error:", err)
		os.Exit(1)
	}
	defer output.Close()
	ex := extractor{config: config, blocks: blocks, rootRegexes: patterns, encoder: json.NewEncoder(bufio.NewWriter(output)), counts: make(map[string]int), dxfCounts: make(map[string]int)}
	// Encoder buffers through its writer only when the wrapper is retained.
	// Re-open as an explicit buffered writer so Flush is guaranteed below.
	buffered := bufio.NewWriter(output)
	ex.encoder = json.NewEncoder(buffered)
	for _, entity := range model {
		if err := ex.writeMatches(entity, "modelspace", nil, identity()); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if entity.Type == "INSERT" && ex.isRoot(value(entity, 2, "")) {
			if err := ex.expand(entity, []string{value(entity, 2, "")}, identity(), 0); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
		}
	}
	if err := buffered.Flush(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	fmt.Println("Read:", inputPath)
	fmt.Println("Output:", outputPath)
	fmt.Println("Extracted objects:")
	for _, name := range sortedKeys(ex.counts) {
		fmt.Printf("  %s: %d\n", name, ex.counts[name])
	}
	for name := range config.LayerMapping {
		if ex.counts[name] == 0 {
			fmt.Printf("WARNING: configured object type was not found: %s\n", name)
		}
	}
	fmt.Println("DXF types:")
	for _, name := range sortedKeys(ex.dxfCounts) {
		fmt.Printf("  %s: %d\n", name, ex.dxfCounts[name])
	}
	missing := make([]string, 0)
	for name, mapping := range config.LayerMapping {
		if mapping.Required && ex.counts[name] == 0 {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		fmt.Fprintln(os.Stderr, "Required object types were not found:", strings.Join(missing, ", "))
		os.Exit(1)
	}
}
