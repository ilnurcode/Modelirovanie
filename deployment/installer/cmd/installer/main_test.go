package main

import (
	"archive/zip"
	"os"
	"path/filepath"
	"testing"
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
