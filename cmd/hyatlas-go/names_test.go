package main

import "testing"

func TestNormalizeName(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{"HyAtlas v4", "hyatlas"},
		{"hyatlas", "hyatlas"},
		{"  HyAtlas   Server ", "hyatlas server"},
		{"HyAtlas 4.6.0", "hyatlas"},
		{"4.6.0", "4.6.0"},
		{"127.0.0.1:19528", "127.0.0.1:19528"},
		{"port 19528", "port 19528"},
		{"(TradingMaker)", "tradingmaker"},
		{"", ""},
	} {
		if got := normalizeName(c.in); got != c.want {
			t.Errorf("normalizeName(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSameEntity(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{
		{"hyatlas server", "HyAtlas v4", true},
		{"HyAtlas", "hyatlas", true},
		{"127.0.0.1:19528", "127.0.0.1:19528", true},
		{"Hermes Desktop", "TradingMaker", false},
		{"hyatlas server", "hermes server", false},
		{"", "hyatlas", false},
	} {
		if got := sameEntity(c.a, c.b); got != c.want {
			t.Errorf("sameEntity(%q, %q) = %v, want %v", c.a, c.b, got, c.want)
		}
	}
}
