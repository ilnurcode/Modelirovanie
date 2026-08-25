package main

import (
	"archive/zip"
	"bufio"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"unicode/utf16"
)

func TestSafeJoinRejectsTraversal(t *testing.T) {
	for _, name := range []string{"../escape", "a/../../escape"} {
		if _, err := safeJoin(t.TempDir(), name); err == nil { t.Fatalf("accepted %q", name) }
	}
}

func TestExtractZipRejectsTraversal(t *testing.T) {
	dir := t.TempDir(); archive := filepath.Join(dir, "bad.zip")
	f, err := os.Create(archive); if err != nil { t.Fatal(err) }; zw := zip.NewWriter(f); w, err := zw.Create("../escape.txt"); if err != nil { t.Fatal(err) }; if _, err := w.Write([]byte("bad")); err != nil { t.Fatal(err) }; if err := zw.Close(); err != nil { t.Fatal(err) }; if err := f.Close(); err != nil { t.Fatal(err) }
	if err := extractZip(archive, filepath.Join(dir, "out")); err == nil { t.Fatal("traversal archive accepted") }
}

func TestCompareVersion(t *testing.T) {
	if compareVersion("1.5.0", "1.4.9") <= 0 || compareVersion("1.5", "1.5.0") != 0 || compareVersion("1.4.9", "1.5.0") >= 0 { t.Fatal("version comparison failed") }
}

func TestDefaultManifestURL(t *testing.T) {
	c, err := manifestSource(commonFlags{})
	if err != nil { t.Fatal(err) }
	if c.manifestURL != defaultManifestURL || c.offlinePath != "" { t.Fatal("default manifest was not selected") }
	if _, err := manifestSource(commonFlags{manifestURL: "https://example.test/manifest.json", offlinePath: "bundle"}); err == nil { t.Fatal("two sources accepted") }
}

func TestFetchVerifyExtractMovesStagedDirectory(t *testing.T) {
	root := t.TempDir(); bundle := filepath.Join(root, "bundle"); appDir := filepath.Join(bundle, "application")
	if err := os.MkdirAll(appDir, 0o755); err != nil { t.Fatal(err) }
	archive := filepath.Join(appDir, "app.zip"); f, err := os.Create(archive); if err != nil { t.Fatal(err) }; zw := zip.NewWriter(f); w, err := zw.Create("consultant.txt"); if err != nil { t.Fatal(err) }; if _, err := w.Write([]byte("ok")); err != nil { t.Fatal(err) }; if err := zw.Close(); err != nil { t.Fatal(err) }; if err := f.Close(); err != nil { t.Fatal(err) }
	hash, size, err := fileHash(archive); if err != nil { t.Fatal(err) }; dest := filepath.Join(root, "app", "0.6.1")
	if err := fetchVerifyExtract(root, "app.zip", bundle, true, "application", hash, size, dest, nil); err != nil { t.Fatal(err) }
	data, err := os.ReadFile(filepath.Join(dest, "consultant.txt")); if err != nil { t.Fatal(err) }; if string(data) != "ok" { t.Fatal("bad extracted content") }
}

func TestRenameRetriesTemporaryWindowsLock(t *testing.T) {
	dir := t.TempDir(); source := filepath.Join(dir, "source"); target := filepath.Join(dir, "target")
	if err := os.WriteFile(source, []byte("ok"), 0o600); err != nil { t.Fatal(err) }
	realRename := renameFile; attempts := 0
	renameFile = func(old, new string) error {
		attempts++
		if attempts == 1 { return os.ErrPermission }
		return realRename(old, new)
	}
	defer func() { renameFile = realRename }()
	if err := renameWithRetry(source, target); err != nil { t.Fatal(err) }
	if attempts != 2 { t.Fatalf("got %d attempts", attempts) }
	if data, err := os.ReadFile(target); err != nil || string(data) != "ok" { t.Fatal("renamed file missing") }
}

func TestAskSource(t *testing.T) {
	args, err := askSource(bufio.NewScanner(strings.NewReader("\n"))); if err != nil || len(args) != 0 { t.Fatal("default online source failed") }
	args, err = askSource(bufio.NewScanner(strings.NewReader("2\nbundle\n"))); if err != nil || len(args) != 2 || args[0] != "--offline-path" || args[1] != "bundle" { t.Fatal("offline source failed") }
}

func TestWindowsShortcutScript(t *testing.T) {
	script := windowsShortcutScript(`C:\Users\O'Brien\1C-Consultant`, false)
	if !strings.Contains(script, `'C:\Users\O''Brien\1C-Consultant'`) { t.Fatal("PowerShell path was not quoted") }
	if !strings.Contains(script, "CreateShortcut") || !strings.Contains(script, "desktopLink") || !strings.Contains(script, "menuLink") { t.Fatal("desktop or Start Menu shortcut missing") }
}

func TestEncodePowerShell(t *testing.T) {
	want := "ярлык 1C-Consultant"; raw, err := base64.StdEncoding.DecodeString(encodePowerShell(want)); if err != nil { t.Fatal(err) }
	words := make([]uint16, len(raw)/2); for i := range words { words[i] = uint16(raw[i*2]) | uint16(raw[i*2+1])<<8 }; if got := string(utf16.Decode(words)); got != want { t.Fatalf("got %q", got) }
}

func TestWriteAndRemoveMacOSApp(t *testing.T) {
	home := t.TempDir(); root := "/tmp/1C Consultant's"; if err := writeMacOSAppAt(home, root, "0.8.0"); err != nil { t.Fatal(err) }
	app := filepath.Join(home, "Applications", "1C-Consultant.app", "Contents"); plist, err := os.ReadFile(filepath.Join(app, "Info.plist")); if err != nil { t.Fatal(err) }
	if !strings.Contains(string(plist), "com.ilnurcode.1c-consultant") || !strings.Contains(string(plist), "0.8.0") { t.Fatal("invalid Info.plist") }
	launcher, err := os.ReadFile(filepath.Join(app, "MacOS", "1C-Consultant")); if err != nil { t.Fatal(err) }; if !strings.Contains(string(launcher), shellQuote(filepath.Join(root, "1C-Consultant.command"))) { t.Fatal("macOS launcher path was not quoted") }
	if err := removeMacOSAppAt(home); err != nil { t.Fatal(err) }; if _, err := os.Stat(filepath.Join(home, "Applications", "1C-Consultant.app")); !errors.Is(err, os.ErrNotExist) { t.Fatal("macOS app was not removed") }
}

func TestExternalAppManaged(t *testing.T) {
	t.Setenv("CONSULTANT_EXTERNAL_APP", "1")
	if !externalAppManaged() { t.Fatal("external app mode was not detected") }
}

func TestInstallerIsPersistedForFutureManagement(t *testing.T) {
	root := t.TempDir(); bundle := filepath.Join(root, "bundle"); sourceDir := filepath.Join(bundle, "installer")
	if err := os.MkdirAll(sourceDir, 0o755); err != nil { t.Fatal(err) }
	osName, arch := platform(); filename := "installer"; if osName == "windows" { filename += ".exe" }; source := filepath.Join(sourceDir, filename)
	if err := os.WriteFile(source, []byte("installer-binary"), 0o755); err != nil { t.Fatal(err) }; hash, size, err := fileHash(source); if err != nil { t.Fatal(err) }
	m := Manifest{Installer: Installer{Version:"0.4.3", Artifacts:[]InstallerArtifact{{OS:osName, Arch:arch, URL:filename, SHA256:hash, Size:size, Filename:filename}}}}
	s := &State{Installers:map[string]Installed{}}
	if err := installInstaller(root, m, s, bundle, true, nil); err != nil { t.Fatal(err) }
	installed := s.Installers["0.4.3"]; if s.ActiveInstaller != "0.4.3" { t.Fatal("installer was not activated") }; if _, err := os.Stat(filepath.Join(installed.Path, installed.Executable)); err != nil { t.Fatal(err) }
}

func TestGraphRollbackAndRemovalTrackAllVersions(t *testing.T) {
	root := t.TempDir(); t.Setenv("CONSULTANT_INSTALL_ROOT", root); id := "erp-test"; v1 := filepath.Join(root, "graphs", id, "cfg", "1"); v2 := filepath.Join(root, "graphs", id, "cfg", "2")
	if err := os.MkdirAll(v1, 0o755); err != nil { t.Fatal(err) }; if err := os.MkdirAll(v2, 0o755); err != nil { t.Fatal(err) }
	s := &State{SchemaVersion:1, Applications:map[string]Installed{}, Installers:map[string]Installed{}, Graphs:map[string]GraphState{id:{Name:"ERP", ActiveVersion:"2", PreviousVersion:"1", Path:v2, Versions:map[string]GraphInstalled{"1":{Path:v1}, "2":{Path:v2}}}}}
	if err := saveState(root, s); err != nil { t.Fatal(err) }; if err := rollbackGraphCommand([]string{"--graph", id}); err != nil { t.Fatal(err) }
	s, err := loadState(root); if err != nil { t.Fatal(err) }; if s.Graphs[id].ActiveVersion != "1" || s.Graphs[id].PreviousVersion != "2" { t.Fatal("graph rollback failed") }
	if err := removeGraphCommand([]string{"--graph", id}); err != nil { t.Fatal(err) }; if _, err := os.Stat(filepath.Join(root, "graphs", id)); !errors.Is(err, os.ErrNotExist) { t.Fatal("graph versions were not removed") }
}

func TestLauncherKeepsInstallerManagementAvailable(t *testing.T) {
	root := t.TempDir(); appDir := filepath.Join(root, "app", "1"); installerDir := filepath.Join(root, "installer", "1"); if err := os.MkdirAll(appDir, 0o755); err != nil { t.Fatal(err) }; if err := os.MkdirAll(installerDir, 0o755); err != nil { t.Fatal(err) }
	appName, installerName := "consultant", "installer"; launcherName := "1c-consultant"; if runtime.GOOS == "windows" { appName += ".exe"; installerName += ".exe"; launcherName = "1C-Consultant.cmd" }
	if err := os.WriteFile(filepath.Join(appDir, appName), []byte("app"), 0o755); err != nil { t.Fatal(err) }; if err := os.WriteFile(filepath.Join(installerDir, installerName), []byte("installer"), 0o755); err != nil { t.Fatal(err) }
	s := &State{ActiveApplication:"1", ActiveInstaller:"1", Applications:map[string]Installed{"1":{Path:appDir, Executable:appName}}, Installers:map[string]Installed{"1":{Path:installerDir, Executable:installerName}}}
	if err := writeLauncher(root, s); err != nil { t.Fatal(err) }; body, err := os.ReadFile(filepath.Join(root, launcherName)); if err != nil { t.Fatal(err) }; text := string(body); if !strings.Contains(text, "CONSULTANT_INSTALL_ROOT") || !strings.Contains(text, installerName) { t.Fatal("management installer is absent from launcher") }
}
