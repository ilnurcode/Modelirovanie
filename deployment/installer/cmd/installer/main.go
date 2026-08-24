package main

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

const (
	installerVersion = "0.2.0"
	defaultManifestURL = "https://github.com/ilnurcode/Modelirovanie/releases/latest/download/manifest.json"
)

type Manifest struct {
	SchemaVersion int         `json:"schema_version"`
	Application   Application `json:"application"`
	Graphs        []Graph     `json:"graphs"`
}

type Application struct {
	Version   string        `json:"version"`
	Artifacts []AppArtifact `json:"artifacts"`
}

type AppArtifact struct {
	OS              string   `json:"os"`
	Arch            string   `json:"arch"`
	URL             string   `json:"url"`
	SHA256          string   `json:"sha256"`
	Size            int64    `json:"size,omitempty"`
	Executable      string   `json:"executable"`
	HealthCheckArgs []string `json:"health_check_args,omitempty"`
}

type Graph struct {
	ID                        string `json:"id"`
	Name                      string `json:"name"`
	ConfigurationVersion      string `json:"configuration_version"`
	GraphVersion              string `json:"graph_version"`
	URL                       string `json:"url"`
	SHA256                    string `json:"sha256"`
	Size                      int64  `json:"size,omitempty"`
	MinimumApplicationVersion string `json:"minimum_application_version"`
}

type State struct {
	SchemaVersion       int                   `json:"schema_version"`
	ActiveApplication   string                `json:"active_application,omitempty"`
	PreviousApplication string                `json:"previous_application,omitempty"`
	Applications        map[string]Installed  `json:"applications"`
	Graphs              map[string]GraphState `json:"graphs"`
	UpdatedAt           string                `json:"updated_at"`
}

type Installed struct {
	Path       string `json:"path"`
	Executable string `json:"executable"`
	Installed  string `json:"installed_at"`
}

type GraphState struct {
	Name                 string `json:"name"`
	ConfigurationVersion string `json:"configuration_version"`
	ActiveVersion        string `json:"active_version"`
	Path                 string `json:"path"`
	Installed            string `json:"installed_at"`
}

type logger struct{ file *os.File }

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "Ошибка:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return menu()
	}
	switch args[0] {
	case "install":
		return installCommand(args[1:])
	case "status":
		return statusCommand(args[1:])
	case "check":
		return checkCommand(args[1:])
	case "rollback":
		return rollbackCommand(args[1:])
	case "remove-graph":
		return removeGraphCommand(args[1:])
	case "uninstall":
		return uninstallCommand(args[1:])
	case "version", "--version", "-version":
		fmt.Println(installerVersion)
		return nil
	case "help", "--help", "-h":
		usage()
		return nil
	default:
		usage()
		return fmt.Errorf("неизвестная команда %q", args[0])
	}
}

func usage() {
	fmt.Print(`1c-consultant-installer commands:
  install       установить/обновить приложение и графы
  status        показать установленные версии
  check         сравнить локальные версии с manifest
  rollback      восстановить предыдущую версию приложения
  remove-graph  удалить выбранный граф
  uninstall     удалить локальную установку

По умолчанию используется manifest последнего GitHub Release.
Другой источник: --manifest https://.../manifest.json или --offline-path <bundle-dir>
Общие параметры: --data-dir <dir>
`)
}

func menu() error {
	in := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("\nУстановка 1C-Consultant\n1. Установить или обновить\n2. Показать версии\n3. Проверить обновления\n4. Откатить приложение\n5. Удалить граф\n6. Удалить 1C-Consultant\n0. Выход\n> ")
		if !in.Scan() { return in.Err() }
		choice := strings.TrimSpace(in.Text())
		switch choice {
		case "0": return nil
		case "1", "3":
			fmt.Print("Manifest URL, путь к offline bundle или Enter для GitHub Release: ")
			if !in.Scan() { return in.Err() }
			source := strings.TrimSpace(in.Text())
			args := []string{}
			if strings.TrimSpace(source) != "" && strings.HasPrefix(strings.TrimSpace(source), "http") { args = append(args, "--manifest", source) }
			if strings.TrimSpace(source) != "" && !strings.HasPrefix(strings.TrimSpace(source), "http") { args = append(args, "--offline-path", source) }
			var err error
			if choice == "3" { err = checkCommand(args) } else { err = installCommand(args) }
			if err != nil { fmt.Println("Ошибка:", err) }
		case "2": if err := statusCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "4": if err := rollbackCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "5":
			fmt.Print("ID графа: "); if !in.Scan() { return in.Err() }
			if err := removeGraphCommand([]string{"--graph", strings.TrimSpace(in.Text())}); err != nil { fmt.Println("Ошибка:", err) }
		case "6": if err := uninstallCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		default: fmt.Println("Неизвестный пункт")
		}
	}
}

type commonFlags struct { dataDir, manifestURL, offlinePath string }

func addCommon(fs *flag.FlagSet, source bool) *commonFlags {
	c := &commonFlags{}
	fs.StringVar(&c.dataDir, "data-dir", "", "каталог установки")
	if source {
		fs.StringVar(&c.manifestURL, "manifest", "", "HTTPS URL manifest.json")
		fs.StringVar(&c.offlinePath, "offline-path", "", "каталог offline bundle")
	}
	return c
}

func installCommand(args []string) error {
	fs := flag.NewFlagSet("install", flag.ContinueOnError)
	c := addCommon(fs, true)
	application := fs.Bool("application", false, "установить приложение")
	graphIDs := fs.String("graphs", "", "ID графов через запятую")
	nonInteractive := fs.Bool("non-interactive", false, "не задавать вопросы")
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	log, err := openLog(root); if err != nil { return err }; defer log.close()
	m, base, offline, err := loadManifest(*c, log); if err != nil { return err }
	selected := csvSet(*graphIDs)
	installApp := *application
	if !*nonInteractive && !installApp && len(selected) == 0 {
		installApp, selected, err = askSelection(m)
		if err != nil { return err }
	}
	if *nonInteractive && !installApp && len(selected) == 0 { return errors.New("задайте --application и/или --graphs") }
	state, err := loadState(root); if err != nil { return err }
	if installApp {
		if err := installApplication(root, m, state, base, offline, log); err != nil { return err }
	}
	for id := range selected {
		g, ok := graphByID(m, id); if !ok { return fmt.Errorf("граф %q отсутствует в manifest", id) }
		if err := installGraph(root, g, state, base, offline, log); err != nil { return err }
	}
	state.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	if err := saveState(root, state); err != nil { return err }
	if state.ActiveApplication != "" { if err := writeLauncher(root, state); err != nil { return err } }
	log.info("Установка завершена")
	return nil
}

func checkCommand(args []string) error {
	fs := flag.NewFlagSet("check", flag.ContinueOnError); c := addCommon(fs, true)
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	log, err := openLog(root); if err != nil { return err }; defer log.close()
	m, _, _, err := loadManifest(*c, log); if err != nil { return err }
	s, err := loadState(root); if err != nil { return err }
	fmt.Printf("Приложение: установлено %s, доступно %s\n", valueOr(s.ActiveApplication, "нет"), m.Application.Version)
	for _, g := range m.Graphs { local := "нет"; if x, ok := s.Graphs[g.ID]; ok { local = x.ActiveVersion }; fmt.Printf("Граф %s: установлен %s, доступен %s\n", g.ID, local, g.GraphVersion) }
	return nil
}

func statusCommand(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError); c := addCommon(fs, false)
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	s, err := loadState(root); if err != nil { return err }
	fmt.Println("Каталог:", root); fmt.Println("Активное приложение:", valueOr(s.ActiveApplication, "не установлено"))
	ids := make([]string, 0, len(s.Graphs)); for id := range s.Graphs { ids = append(ids, id) }; sort.Strings(ids)
	for _, id := range ids { fmt.Printf("Граф %s: %s\n", id, s.Graphs[id].ActiveVersion) }
	return nil
}

func rollbackCommand(args []string) error {
	fs := flag.NewFlagSet("rollback", flag.ContinueOnError); c := addCommon(fs, false)
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	log, err := openLog(root); if err != nil { return err }; defer log.close()
	s, err := loadState(root); if err != nil { return err }
	if s.PreviousApplication == "" { return errors.New("предыдущая версия отсутствует") }
	if _, ok := s.Applications[s.PreviousApplication]; !ok { return errors.New("каталог предыдущей версии отсутствует в состоянии") }
	s.ActiveApplication, s.PreviousApplication = s.PreviousApplication, s.ActiveApplication
	s.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	if err := saveState(root, s); err != nil { return err }; if err := writeLauncher(root, s); err != nil { return err }
	log.info("Выполнен откат на " + s.ActiveApplication); return nil
}

func removeGraphCommand(args []string) error {
	fs := flag.NewFlagSet("remove-graph", flag.ContinueOnError); c := addCommon(fs, false); id := fs.String("graph", "", "ID графа")
	if err := fs.Parse(args); err != nil { return err }; if !safeSegment(*id) { return errors.New("некорректный ID графа") }
	root, err := dataDir(c.dataDir); if err != nil { return err }; s, err := loadState(root); if err != nil { return err }
	g, ok := s.Graphs[*id]; if !ok { return fmt.Errorf("граф %q не установлен", *id) }
	if !within(filepath.Join(root, "graphs"), g.Path) { return errors.New("путь графа выходит из каталога установки") }
	if err := os.RemoveAll(g.Path); err != nil { return err }; delete(s.Graphs, *id); s.UpdatedAt = time.Now().UTC().Format(time.RFC3339); return saveState(root, s)
}

func uninstallCommand(args []string) error {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError); c := addCommon(fs, false); yes := fs.Bool("yes", false, "подтвердить удаление")
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	if !within(filepath.Dir(root), root) || filepath.Clean(root) == filepath.Clean(filepath.VolumeName(root)+string(filepath.Separator)) { return errors.New("небезопасный каталог удаления") }
	if _, err := os.Stat(filepath.Join(root, "config", "installed.json")); err != nil { return errors.New("installed.json не найден; удаление отменено") }
	if !*yes { fmt.Printf("Удалить %s? [y/N]: ", root); var answer string; fmt.Scanln(&answer); if !strings.EqualFold(answer, "y") && !strings.EqualFold(answer, "yes") { return errors.New("удаление отменено") } }
	return os.RemoveAll(root)
}

func installApplication(root string, m Manifest, s *State, base string, offline bool, log *logger) error {
	osName, arch := platform(); var a *AppArtifact
	for i := range m.Application.Artifacts { if m.Application.Artifacts[i].OS == osName && m.Application.Artifacts[i].Arch == arch { a = &m.Application.Artifacts[i]; break } }
	if a == nil { return fmt.Errorf("приложение %s/%s не поддерживается manifest", osName, arch) }
	if !safeSegment(m.Application.Version) { return errors.New("некорректная версия приложения") }
	dest := filepath.Join(root, "app", m.Application.Version)
	if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) {
		if err := fetchVerifyExtract(root, a.URL, base, offline, "application", a.SHA256, a.Size, dest, log); err != nil { return err }
	}
	exe, err := safeJoin(dest, a.Executable); if err != nil { return err }
	if err := healthCheck(exe, dest, a.HealthCheckArgs); err != nil { return fmt.Errorf("проверка запуска: %w", err) }
	if s.ActiveApplication != "" && s.ActiveApplication != m.Application.Version { s.PreviousApplication = s.ActiveApplication }
	s.ActiveApplication = m.Application.Version
	s.Applications[m.Application.Version] = Installed{Path: dest, Executable: a.Executable, Installed: time.Now().UTC().Format(time.RFC3339)}
	return nil
}

func installGraph(root string, g Graph, s *State, base string, offline bool, log *logger) error {
	if !safeSegment(g.ID) || !safeSegment(g.ConfigurationVersion) || !safeSegment(g.GraphVersion) { return errors.New("некорректный идентификатор графа") }
	active := s.ActiveApplication; if active == "" { return errors.New("сначала установите приложение") }
	if compareVersion(active, g.MinimumApplicationVersion) < 0 { return fmt.Errorf("граф %s требует приложение >= %s", g.ID, g.MinimumApplicationVersion) }
	dest := filepath.Join(root, "graphs", g.ID, g.ConfigurationVersion, g.GraphVersion)
	if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) { if err := fetchVerifyExtract(root, g.URL, base, offline, "graphs", g.SHA256, g.Size, dest, log); err != nil { return err } }
	s.Graphs[g.ID] = GraphState{Name:g.Name, ConfigurationVersion:g.ConfigurationVersion, ActiveVersion:g.GraphVersion, Path:dest, Installed:time.Now().UTC().Format(time.RFC3339)}
	return nil
}

func fetchVerifyExtract(root, rawURL, base string, offline bool, kind, wantHash string, wantSize int64, dest string, log *logger) error {
	tmpDir := filepath.Join(root, "temp"); if err := os.MkdirAll(tmpDir, 0o755); err != nil { return err }
	f, err := os.CreateTemp(tmpDir, "download-*"); if err != nil { return err }; archive := f.Name(); f.Close(); defer os.Remove(archive)
	if err := fetch(rawURL, base, offline, kind, archive); err != nil { return err }
	gotHash, size, err := fileHash(archive); if err != nil { return err }
	if wantSize > 0 && size != wantSize { return fmt.Errorf("размер пакета: ожидалось %d, получено %d", wantSize, size) }
	if !strings.EqualFold(gotHash, wantHash) { return fmt.Errorf("SHA-256 не совпал: ожидалось %s, получено %s", wantHash, gotHash) }
	log.info("SHA-256 подтвержден для " + filepath.Base(rawURL))
	stage, err := os.MkdirTemp(tmpDir, "extract-*"); if err != nil { return err }; defer os.RemoveAll(stage)
	payload := filepath.Join(stage, "payload"); if err := os.MkdirAll(payload, 0o755); err != nil { return err }
	if err := extract(archive, rawURL, payload); err != nil { return err }
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil { return err }
	return os.Rename(payload, dest)
}

func fetch(rawURL, base string, offline bool, kind, target string) error {
	var src io.ReadCloser
	if offline {
		u, _ := url.Parse(rawURL); name := filepath.Base(u.Path); if name == "." || name == string(filepath.Separator) { name = filepath.Base(rawURL) }
		candidates := []string{filepath.Join(base, kind, name), filepath.Join(base, name)}
		var f *os.File; var err error
		for _, p := range candidates { f, err = os.Open(p); if err == nil { break } }
		if f == nil { return fmt.Errorf("offline-артефакт %q не найден", name) }; src = f
	} else {
		u, err := url.Parse(rawURL); if err != nil || u.Scheme != "https" { return errors.New("артефакт должен иметь HTTPS URL") }
		client := secureHTTPClient(30 * time.Minute); resp, err := client.Get(rawURL); if err != nil { return err }; if resp.StatusCode != http.StatusOK { resp.Body.Close(); return fmt.Errorf("HTTP %s", resp.Status) }; src = resp.Body
	}
	defer src.Close(); out, err := os.OpenFile(target, os.O_WRONLY|os.O_TRUNC, 0o600); if err != nil { return err }; _, copyErr := io.Copy(out, src); closeErr := out.Close(); if copyErr != nil { return copyErr }; return closeErr
}

func extract(archive, sourceName, dest string) error {
	name := strings.ToLower(sourceName)
	if strings.HasSuffix(name, ".zip") { return extractZip(archive, dest) }
	if strings.HasSuffix(name, ".tar.gz") || strings.HasSuffix(name, ".tgz") { return extractTarGz(archive, dest) }
	return errors.New("поддерживаются только .zip и .tar.gz")
}

func extractZip(path, dest string) error {
	r, err := zip.OpenReader(path); if err != nil { return err }; defer r.Close()
	for _, f := range r.File {
		target, err := safeJoin(dest, f.Name); if err != nil { return err }
		if f.FileInfo().Mode()&os.ModeSymlink != 0 { return errors.New("символические ссылки в архиве запрещены") }
		if f.FileInfo().IsDir() { if err := os.MkdirAll(target, 0o755); err != nil { return err }; continue }
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil { return err }; in, err := f.Open(); if err != nil { return err }; out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, f.Mode().Perm()); if err != nil { in.Close(); return err }; _, copyErr := io.Copy(out, in); in.Close(); closeErr := out.Close(); if copyErr != nil { return copyErr }; if closeErr != nil { return closeErr }
	}
	return nil
}

func extractTarGz(path, dest string) error {
	f, err := os.Open(path); if err != nil { return err }; defer f.Close(); gz, err := gzip.NewReader(f); if err != nil { return err }; defer gz.Close(); tr := tar.NewReader(gz)
	for { h, err := tr.Next(); if errors.Is(err, io.EOF) { return nil }; if err != nil { return err }; target, err := safeJoin(dest, h.Name); if err != nil { return err }; switch h.Typeflag { case tar.TypeDir: if err := os.MkdirAll(target, 0o755); err != nil { return err }; case tar.TypeReg: if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil { return err }; out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(h.Mode).Perm()); if err != nil { return err }; _, copyErr := io.Copy(out, tr); closeErr := out.Close(); if copyErr != nil { return copyErr }; if closeErr != nil { return closeErr }; default: return fmt.Errorf("недопустимый тип элемента архива %q", h.Name) } }
}

func loadManifest(c commonFlags, log *logger) (Manifest, string, bool, error) {
	var m Manifest; var data []byte; var err error; var base string
	c, err = manifestSource(c); if err != nil { return m, "", false, err }
	offline := c.offlinePath != ""
	if offline { base, err = filepath.Abs(c.offlinePath); if err == nil { data, err = os.ReadFile(filepath.Join(base, "manifest.json")) } } else { u, parseErr := url.Parse(c.manifestURL); if parseErr != nil || u.Scheme != "https" { return m, "", false, errors.New("manifest должен иметь HTTPS URL") }; base = c.manifestURL; client := secureHTTPClient(30*time.Second); var resp *http.Response; resp, err = client.Get(c.manifestURL); if err == nil { defer resp.Body.Close(); if resp.StatusCode != http.StatusOK { err = fmt.Errorf("manifest: HTTP %s", resp.Status) } else { data, err = io.ReadAll(io.LimitReader(resp.Body, 4<<20)) } } }
	if err != nil { return m, base, offline, err }; if err := json.Unmarshal(data, &m); err != nil { return m, base, offline, err }; if m.SchemaVersion != 1 { return m, base, offline, fmt.Errorf("schema_version %d не поддерживается", m.SchemaVersion) }; if err := validateManifest(m); err != nil { return m, base, offline, err }
	log.info("Manifest: " + safeLocation(valueOr(c.manifestURL, c.offlinePath))); return m, base, offline, nil
}

func manifestSource(c commonFlags) (commonFlags, error) {
	if c.manifestURL != "" && c.offlinePath != "" { return c, errors.New("задайте только один источник: --manifest или --offline-path") }
	if c.manifestURL == "" && c.offlinePath == "" { c.manifestURL = defaultManifestURL }
	return c, nil
}

func validateManifest(m Manifest) error {
	if !safeSegment(m.Application.Version) { return errors.New("manifest: некорректная версия приложения") }
	for _, a := range m.Application.Artifacts { if a.OS == "" || a.Arch == "" || a.URL == "" || a.Executable == "" || !validHash(a.SHA256) { return errors.New("manifest: неполный артефакт приложения") }; if _, err := safeJoin("root", a.Executable); err != nil { return errors.New("manifest: небезопасный путь executable") } }
	seen := map[string]bool{}; for _, g := range m.Graphs { if !safeSegment(g.ID) || seen[g.ID] || g.URL == "" || !validHash(g.SHA256) { return fmt.Errorf("manifest: некорректный граф %q", g.ID) }; seen[g.ID] = true }
	return nil
}

func loadState(root string) (*State, error) {
	s := &State{SchemaVersion:1, Applications:map[string]Installed{}, Graphs:map[string]GraphState{}}
	data, err := os.ReadFile(filepath.Join(root, "config", "installed.json")); if errors.Is(err, os.ErrNotExist) { return s, nil }; if err != nil { return nil, err }; if err := json.Unmarshal(data, s); err != nil { return nil, err }; if s.Applications == nil { s.Applications = map[string]Installed{} }; if s.Graphs == nil { s.Graphs = map[string]GraphState{} }; return s, nil
}

func saveState(root string, s *State) error {
	dir := filepath.Join(root, "config"); if err := os.MkdirAll(dir, 0o755); err != nil { return err }; data, err := json.MarshalIndent(s, "", "  "); if err != nil { return err }; tmp, err := os.CreateTemp(dir, "installed-*.tmp"); if err != nil { return err }; name := tmp.Name(); defer os.Remove(name); if err := tmp.Chmod(0o600); err != nil { tmp.Close(); return err }; if _, err := tmp.Write(append(data, '\n')); err != nil { tmp.Close(); return err }; if err := tmp.Close(); err != nil { return err }
	target, backup := filepath.Join(dir, "installed.json"), filepath.Join(dir, "installed.json.bak"); _ = os.Remove(backup)
	if err := os.Rename(target, backup); err != nil && !errors.Is(err, os.ErrNotExist) { return err }
	if err := os.Rename(name, target); err != nil { _ = os.Rename(backup, target); return err }
	_ = os.Remove(backup); return nil
}

func writeLauncher(root string, s *State) error {
	a, ok := s.Applications[s.ActiveApplication]; if !ok { return errors.New("активное приложение отсутствует") }; exe, err := safeJoin(a.Path, a.Executable); if err != nil { return err }
	if !within(filepath.Join(root, "app"), a.Path) { return errors.New("путь приложения выходит из каталога установки") }
	if runtime.GOOS == "windows" { body := "@echo off\r\nset \"CONSULTANT_DATA_DIR="+root+"\"\r\ncd /d \""+a.Path+"\"\r\n\""+exe+"\" %*\r\n"; return os.WriteFile(filepath.Join(root, "1C-Consultant.cmd"), []byte(body), 0o755) }
	escapedRoot := strings.ReplaceAll(root, "'", "'\\''"); body := "#!/bin/sh\nexport CONSULTANT_DATA_DIR='"+escapedRoot+"'\ncd '"+strings.ReplaceAll(a.Path, "'", "'\\''")+"'\nexec '"+strings.ReplaceAll(exe, "'", "'\\''")+"' \"$@\"\n"; return os.WriteFile(filepath.Join(root, "1c-consultant"), []byte(body), 0o755)
}

func dataDir(override string) (string, error) {
	if override != "" { return filepath.Abs(override) }
	switch runtime.GOOS { case "windows": base := os.Getenv("LOCALAPPDATA"); if base == "" { return "", errors.New("LOCALAPPDATA не задан") }; return filepath.Join(base, "1C-Consultant"), nil; case "darwin": home, err := os.UserHomeDir(); if err != nil { return "", err }; return filepath.Join(home, "Library", "Application Support", "1C-Consultant"), nil; default: if x := os.Getenv("XDG_DATA_HOME"); x != "" { return filepath.Join(x, "1c-consultant"), nil }; home, err := os.UserHomeDir(); if err != nil { return "", err }; return filepath.Join(home, ".local", "share", "1c-consultant"), nil }
}

func openLog(root string) (*logger, error) { dir := filepath.Join(root, "logs"); if err := os.MkdirAll(dir, 0o755); err != nil { return nil, err }; f, err := os.OpenFile(filepath.Join(dir, "installer.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); if err != nil { return nil, err }; l := &logger{file:f}; l.info("installer "+installerVersion+" запущен"); return l, nil }
func (l *logger) info(message string) { line := time.Now().UTC().Format(time.RFC3339)+" "+message; fmt.Println(message); if l != nil && l.file != nil { fmt.Fprintln(l.file, line) } }
func (l *logger) close() { if l != nil && l.file != nil { l.file.Close() } }

func askSelection(m Manifest) (bool, map[string]bool, error) {
	in := bufio.NewScanner(os.Stdin); fmt.Printf("Установить приложение %s? [Y/n]: ", m.Application.Version); if !in.Scan() { return false, nil, in.Err() }; app := !strings.EqualFold(strings.TrimSpace(in.Text()), "n")
	fmt.Println("Доступные графы:"); for i, g := range m.Graphs { fmt.Printf("[%d] %s %s graph %s (%s)\n", i+1, g.Name, g.ConfigurationVersion, g.GraphVersion, g.ID) }; fmt.Print("Введите номера через запятую или Enter для установки всех: "); if !in.Scan() { return false, nil, in.Err() }; selected := map[string]bool{}; answer := strings.TrimSpace(in.Text()); if answer == "" { for _, g := range m.Graphs { selected[g.ID] = true }; return app, selected, nil }; for item := range csvSet(answer) { var n int; if _, err := fmt.Sscanf(item, "%d", &n); err != nil || n < 1 || n > len(m.Graphs) { return false, nil, fmt.Errorf("некорректный номер графа %q", item) }; selected[m.Graphs[n-1].ID] = true }; return app, selected, nil
}

func graphByID(m Manifest, id string) (Graph, bool) { for _, g := range m.Graphs { if g.ID == id { return g, true } }; return Graph{}, false }
func csvSet(v string) map[string]bool { out := map[string]bool{}; for _, x := range strings.Split(v, ",") { x = strings.TrimSpace(x); if x != "" { out[x] = true } }; return out }
func platform() (string, string) { osName := runtime.GOOS; if osName == "darwin" { osName = "macos" }; arch := runtime.GOARCH; if arch == "amd64" { arch = "x64" }; return osName, arch }
func healthCheck(exe, cwd string, args []string) error { if len(args) == 0 { args = []string{"--version"} }; cmd := exec.Command(exe, args...); cmd.Dir = cwd; cmd.Stdout = os.Stdout; cmd.Stderr = os.Stderr; return cmd.Run() }
func fileHash(path string) (string, int64, error) { f, err := os.Open(path); if err != nil { return "", 0, err }; defer f.Close(); h := sha256.New(); n, err := io.Copy(h, f); return hex.EncodeToString(h.Sum(nil)), n, err }
func validHash(v string) bool { if len(v) != 64 { return false }; _, err := hex.DecodeString(v); return err == nil }
func safeSegment(v string) bool { if v == "" || v == "." || v == ".." { return false }; return !strings.ContainsAny(v, `/\\:`) }
func safeJoin(root, name string) (string, error) { cleaned := filepath.Clean(filepath.FromSlash(name)); if filepath.IsAbs(cleaned) || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(filepath.Separator)) { return "", fmt.Errorf("небезопасный путь %q", name) }; target := filepath.Join(root, cleaned); rel, err := filepath.Rel(root, target); if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) { return "", fmt.Errorf("путь выходит из каталога: %q", name) }; return target, nil }
func within(root, target string) bool { rel, err := filepath.Rel(filepath.Clean(root), filepath.Clean(target)); return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) }
func safeLocation(raw string) string { u, err := url.Parse(raw); if err == nil && u.Scheme != "" { u.RawQuery = ""; u.Fragment = ""; return u.String() }; return raw }
func secureHTTPClient(timeout time.Duration) *http.Client { return &http.Client{Timeout: timeout, CheckRedirect: func(req *http.Request, _ []*http.Request) error { if req.URL.Scheme != "https" { return errors.New("перенаправление с HTTPS запрещено") }; return nil }} }
func valueOr(v, fallback string) string { if v == "" { return fallback }; return v }
func compareVersion(a, b string) int { aa, bb := strings.Split(a, "."), strings.Split(b, "."); n := len(aa); if len(bb) > n { n = len(bb) }; for i:=0; i<n; i++ { var x,y int; if i<len(aa) { fmt.Sscanf(aa[i], "%d", &x) }; if i<len(bb) { fmt.Sscanf(bb[i], "%d", &y) }; if x<y { return -1 }; if x>y { return 1 } }; return 0 }
