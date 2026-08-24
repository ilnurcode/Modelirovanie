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
