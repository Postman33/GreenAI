// dxf_layer_audit writes a compact inventory of every layer and entity type
// present in an ASCII DXF. It reads both modelspace entities and block
// definitions, which makes it useful for checking a bound drawing before a
// semantic extraction config is changed.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"golang.org/x/text/encoding/charmap"
)

type typeCounts map[string]int

type layerStats struct {
	Layer    string         `json:"layer"`
	Total    int            `json:"total"`
	Types    typeCounts     `json:"types"`
	Sections map[string]int `json:"sections"`
}

type report struct {
	Input       string       `json:"input"`
	EntityCount int          `json:"entity_count"`
	LayerCount  int          `json:"layer_count"`
	Layers      []layerStats `json:"layers"`
}

func pair(scanner *bufio.Scanner) (int, string, error) {
	if !scanner.Scan() {
		if scanner.Err() != nil {
			return 0, "", scanner.Err()
		}
		return 0, "", io.EOF
	}
	codeText := strings.TrimSpace(scanner.Text())
	if !scanner.Scan() {
		return 0, "", io.ErrUnexpectedEOF
	}
	code, err := strconv.Atoi(codeText)
	if err != nil {
		return 0, "", fmt.Errorf("invalid group code %q: %w", codeText, err)
	}
	return code, strings.TrimSpace(scanner.Text()), nil
}

func decode(value string) string {
	if utf8.ValidString(value) {
		return value
	}
	decoded, err := charmap.Windows1251.NewDecoder().String(value)
	if err != nil {
		return value
	}
	return decoded
}

func main() {
	output := flag.String("output", "dxf_layer_inventory.json", "output JSON path")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: dxf_layer_audit [--output report.json] input.dxf")
		os.Exit(2)
	}
	input := flag.Arg(0)
	file, err := os.Open(input)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 8*1024*1024)
	stats := make(map[string]*layerStats)
	section := ""
	pendingSection := false
	entityType, entityLayer := "", "0"
	entitySection := ""
	entityCount := 0

	finishEntity := func() {
		if entityType == "" || (entitySection != "ENTITIES" && entitySection != "BLOCKS") {
			entityType = ""
			entityLayer = "0"
			return
		}
		entry := stats[entityLayer]
		if entry == nil {
			entry = &layerStats{
				Layer:    entityLayer,
				Types:    make(typeCounts),
				Sections: make(map[string]int),
			}
			stats[entityLayer] = entry
		}
		entry.Total++
		entry.Types[entityType]++
		entry.Sections[entitySection]++
		entityCount++
		entityType = ""
		entityLayer = "0"
	}

	for {
		code, raw, readErr := pair(scanner)
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			fmt.Fprintln(os.Stderr, readErr)
			os.Exit(1)
		}
		value := decode(raw)
		if pendingSection && code == 2 {
			section = strings.ToUpper(value)
			pendingSection = false
			continue
		}
		if code == 0 {
			finishEntity()
			switch strings.ToUpper(value) {
			case "SECTION":
				pendingSection = true
			case "ENDSEC":
				section = ""
			default:
				if section == "ENTITIES" || section == "BLOCKS" {
					entityType = strings.ToUpper(value)
					entitySection = section
				}
			}
			continue
		}
		if code == 8 && entityType != "" {
			entityLayer = value
		}
	}
	finishEntity()

	layers := make([]layerStats, 0, len(stats))
	for _, entry := range stats {
		layers = append(layers, *entry)
	}
	sort.Slice(layers, func(i, j int) bool {
		if layers[i].Total == layers[j].Total {
			return layers[i].Layer < layers[j].Layer
		}
		return layers[i].Total > layers[j].Total
	})

	result := report{
		Input:       input,
		EntityCount: entityCount,
		LayerCount:  len(layers),
		Layers:      layers,
	}
	data, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.WriteFile(*output, append(data, '\n'), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("Input: %s\nEntities: %d\nLayers: %d\nOutput: %s\n", input, entityCount, len(layers), *output)
}
