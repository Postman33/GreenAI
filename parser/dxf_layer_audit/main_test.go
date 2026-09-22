package main

import (
	"bufio"
	"io"
	"strings"
	"testing"
)

func TestPairReadsDXFGroup(t *testing.T) {
	scanner := bufio.NewScanner(strings.NewReader("  8\nWater\n"))
	code, value, err := pair(scanner)
	if err != nil {
		t.Fatal(err)
	}
	if code != 8 || value != "Water" {
		t.Fatalf("unexpected pair: %d %q", code, value)
	}
	_, _, err = pair(scanner)
	if err != io.EOF {
		t.Fatalf("expected EOF, got %v", err)
	}
}

func TestDecodeKeepsUTF8(t *testing.T) {
	value := "Водопровод"
	if decode(value) != value {
		t.Fatalf("valid UTF-8 changed: %q", decode(value))
	}
}
