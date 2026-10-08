package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// touchModel makes dir hold the BGE model file, as an installer would.
func touchModel(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, modelFileName), []byte("onnx"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// findModelDir is tested with temp dirs standing in for the cwd, the executable's
// directory and the installer's default, so no test changes the process cwd.
func TestFindModelDirSearchOrder(t *testing.T) {
	root := t.TempDir()
	cwd := filepath.Join(root, "cwd")
	exe := filepath.Join(root, "exe")
	inst := filepath.Join(root, "inst")
	cwdModels := filepath.Join(cwd, "models")
	exeModels := filepath.Join(exe, "models")
	override := filepath.Join(root, "explicit")

	// Each case builds its own layout under fresh subdirs.
	cases := []struct {
		name     string
		setup    func(t *testing.T)
		override string
		want     string
		tried    []string
	}{
		{
			name: "cwd beats exe and installer",
			setup: func(t *testing.T) {
				touchModel(t, cwdModels)
				touchModel(t, exeModels)
				touchModel(t, inst)
			},
			want:  cwdModels,
			tried: []string{cwdModels, exeModels, inst},
		},
		{
			name: "exe dir beats installer when cwd is empty",
			setup: func(t *testing.T) {
				touchModel(t, exeModels)
				touchModel(t, inst)
			},
			want:  exeModels,
			tried: []string{cwdModels, exeModels, inst},
		},
		{
			name:  "installer default when nothing beside the binary",
			setup: func(t *testing.T) { touchModel(t, inst) },
			want:  inst,
			tried: []string{cwdModels, exeModels, inst},
		},
		{
			name: "a directory without the model file is skipped",
			setup: func(t *testing.T) {
				if err := os.MkdirAll(cwdModels, 0o755); err != nil {
					t.Fatal(err)
				}
				touchModel(t, exeModels)
			},
			want:  exeModels,
			tried: []string{cwdModels, exeModels, inst},
		},
		{
			name: "override is the only candidate, even when a default has the model",
			setup: func(t *testing.T) {
				touchModel(t, cwdModels)
				if err := os.MkdirAll(override, 0o755); err != nil {
					t.Fatal(err)
				}
			},
			override: override,
			want:     override,
			tried:    []string{override},
		},
		{
			name: "override with the model is used",
			setup: func(t *testing.T) {
				touchModel(t, override)
				touchModel(t, cwdModels)
			},
			override: override,
			want:     override,
			tried:    []string{override},
		},
		{
			name:  "nothing found: first candidate is returned and all are listed",
			setup: func(t *testing.T) {},
			want:  cwdModels,
			tried: []string{cwdModels, exeModels, inst},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			// Fresh layout per case: remove the previous case's tree.
			_ = os.RemoveAll(root)
			for _, d := range []string{cwd, exe, inst} {
				if err := os.MkdirAll(d, 0o755); err != nil {
					t.Fatal(err)
				}
			}
			c.setup(t)
			got, tried := findModelDir(c.override, cwd, exe, inst)
			if got != c.want {
				t.Errorf("dir = %q, want %q", got, c.want)
			}
			if !reflect.DeepEqual(tried, c.tried) {
				t.Errorf("tried = %v, want %v", tried, c.tried)
			}
		})
	}
}

// With no exe or installer directory known, the cwd is still the one candidate.
func TestFindModelDirWithoutOptionalBases(t *testing.T) {
	cwd := t.TempDir()
	got, tried := findModelDir("", cwd, "", "")
	if want := filepath.Join(cwd, "models"); got != want || len(tried) != 1 {
		t.Errorf("got %q tried %v, want %q as the only candidate", got, tried, want)
	}
}

// The installer defaults must match scripts/install.sh: ~/.hyatlas/models on
// Unix, %LOCALAPPDATA%\hyatlas\models on Windows.
func TestInstallModelDirMatchesInstaller(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	if got, want := installModelDir("linux"), filepath.Join(home, ".hyatlas", "models"); got != want {
		t.Errorf("linux: %q, want %q", got, want)
	}
	if got, want := installModelDir("darwin"), filepath.Join(home, ".hyatlas", "models"); got != want {
		t.Errorf("darwin: %q, want %q", got, want)
	}

	local := t.TempDir()
	t.Setenv("LOCALAPPDATA", local)
	if got, want := installModelDir("windows"), filepath.Join(local, "hyatlas", "models"); got != want {
		t.Errorf("windows with LOCALAPPDATA: %q, want %q", got, want)
	}
	t.Setenv("LOCALAPPDATA", "")
	if got, want := installModelDir("windows"), filepath.Join(home, "AppData", "Local", "hyatlas", "models"); got != want {
		t.Errorf("windows without LOCALAPPDATA: %q, want %q", got, want)
	}
}

// HYATLAS_ALLOWED_HOSTS is read in resolveRuntime: comma-separated, normalised,
// and empty entries dropped.
func TestResolveRuntimeReadsAllowedHosts(t *testing.T) {
	clearRuntimeEnv(t)
	if got := resolveRuntime().AllowedHosts; len(got) != 0 {
		t.Errorf("unset: AllowedHosts = %v, want none", got)
	}
	t.Setenv("HYATLAS_ALLOWED_HOSTS", " Hyatlas.LAN , ,other.example. ")
	want := []string{"hyatlas.lan", "other.example"}
	if got := resolveRuntime().AllowedHosts; !reflect.DeepEqual(got, want) {
		t.Errorf("AllowedHosts = %v, want %v", got, want)
	}
}
