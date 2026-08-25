package main

import (
	"archive/zip"
	"bufio"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
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
