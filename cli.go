package main

import (
	"fmt"
	"io"
)

// usageText is the help printed for --help and for unknown arguments. It names
// the environment-variable configuration instead of flags because settings are
// read from HYATLAS_* variables, not from the command line.
const usageText = `hyatlas-go %s — HyAtlas memory server

Usage:
  hyatlas-go             start the server (configured by HYATLAS_* environment variables)
  hyatlas-go --version   print the version
  hyatlas-go --help      print this help

Settings are environment variables, listed in the README:
https://github.com/tuancookiez-hub/HyAtlas-Memory#readme
`

// handleArgs answers the few command-line words the binary understands and reports
// whether the process should exit instead of starting the server. Unknown arguments
// are refused rather than ignored: before this check an unrecognised flag silently
// started a second server, which is the wrong outcome for a typo.
func handleArgs(args []string, out io.Writer) (exit bool, code int) {
	if len(args) == 0 {
		return false, 0
	}
	switch args[0] {
	case "--version", "-version", "-v", "version":
		fmt.Fprintf(out, "hyatlas-go %s\n", Version)
		return true, 0
	case "--help", "-help", "-h", "help":
		fmt.Fprintf(out, usageText, Version)
		return true, 0
	}
	fmt.Fprintf(out, "hyatlas-go: unknown argument %q\n", args[0])
	fmt.Fprintf(out, usageText, Version)
	return true, 2
}
