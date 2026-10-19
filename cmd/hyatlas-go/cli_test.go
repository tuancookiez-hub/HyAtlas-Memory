package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestHandleArgs(t *testing.T) {
	tests := []struct {
		name     string
		args     []string
		wantExit bool
		wantCode int
		contains string
	}{
		{name: "no args starts the server", args: nil, wantExit: false, wantCode: 0},
		{name: "--version", args: []string{"--version"}, wantExit: true, wantCode: 0, contains: Version},
		{name: "-version", args: []string{"-version"}, wantExit: true, wantCode: 0, contains: Version},
		{name: "-v", args: []string{"-v"}, wantExit: true, wantCode: 0, contains: Version},
		{name: "version", args: []string{"version"}, wantExit: true, wantCode: 0, contains: Version},
		{name: "--help", args: []string{"--help"}, wantExit: true, wantCode: 0, contains: "Usage:"},
		{name: "-help", args: []string{"-help"}, wantExit: true, wantCode: 0, contains: "Usage:"},
		{name: "-h", args: []string{"-h"}, wantExit: true, wantCode: 0, contains: "Usage:"},
		{name: "help", args: []string{"help"}, wantExit: true, wantCode: 0, contains: "Usage:"},
		{name: "unknown argument", args: []string{"--bogus"}, wantExit: true, wantCode: 2, contains: "unknown argument"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var out bytes.Buffer
			exit, code := handleArgs(tc.args, &out)
			if exit != tc.wantExit {
				t.Errorf("exit = %v, want %v", exit, tc.wantExit)
			}
			if code != tc.wantCode {
				t.Errorf("code = %d, want %d", code, tc.wantCode)
			}
			if tc.contains != "" && !strings.Contains(out.String(), tc.contains) {
				t.Errorf("output %q does not contain %q", out.String(), tc.contains)
			}
			if tc.wantExit && tc.wantCode == 0 && out.Len() == 0 {
				t.Errorf("expected output for exiting argument %q", tc.args)
			}
		})
	}
}
