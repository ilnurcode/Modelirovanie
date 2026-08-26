package main

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html"
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
	"unicode/utf16"
)

const (
	installerVersion = "0.5.1"
	defaultManifestURL = "https://github.com/ilnurcode/Modelirovanie/releases/latest/download/manifest.json"
)

type Manifest struct {
	SchemaVersion int         `json:"schema_version"`
	Application   Application `json:"application"`
	Installer     Installer   `json:"installer"`
	Pi            Pi          `json:"pi"`
	Graphs        []Graph     `json:"graphs"`
}

type Pi struct {
	Version       string           `json:"version"`
	Package       string           `json:"package"`
	NodeVersion   string           `json:"node_version"`
	NodeArtifacts []NodeArtifact   `json:"node_artifacts"`
}

type NodeArtifact struct {
	OS         string `json:"os"`
	Arch       string `json:"arch"`
	URL        string `json:"url"`
	SHA256     string `json:"sha256"`
	Size       int64  `json:"size,omitempty"`
	Node       string `json:"node"`
	NPM        string `json:"npm"`
}

type Installer struct {
	Version   string              `json:"version"`
	Artifacts []InstallerArtifact `json:"artifacts"`
}

type InstallerArtifact struct {
	OS       string `json:"os"`
	Arch     string `json:"arch"`
	URL      string `json:"url"`
	SHA256   string `json:"sha256"`
	Size     int64  `json:"size,omitempty"`
	Filename string `json:"filename"`
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
	ActiveInstaller     string                `json:"active_installer,omitempty"`
	Applications        map[string]Installed  `json:"applications"`
	Installers           map[string]Installed  `json:"installers"`
	Graphs              map[string]GraphState `json:"graphs"`
	Pi                  *PiInstalled          `json:"pi,omitempty"`
	UpdatedAt           string                `json:"updated_at"`
}

type PiInstalled struct {
	Version        string `json:"version"`
	NodeVersion    string `json:"node_version"`
	NodeExecutable string `json:"node_executable"`
	CLI            string `json:"cli"`
	Installed      string `json:"installed_at"`
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
	PreviousVersion      string                    `json:"previous_version,omitempty"`
	Versions             map[string]GraphInstalled `json:"versions,omitempty"`
}

type GraphInstalled struct {
	Path      string `json:"path"`
	Installed string `json:"installed_at"`
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
	case "rollback-graph":
		return rollbackGraphCommand(args[1:])
	case "install-pi":
		return installPiCommand(args[1:])
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
	  rollback-graph восстановить предыдущую версию графа
	  install-pi    установить или обновить Pi и управляемый Node.js
  uninstall     удалить локальную установку

По умолчанию используется manifest последнего GitHub Release.
Другой источник: --manifest https://.../manifest.json или --offline-path <bundle-dir>
Общие параметры: --data-dir <dir>
`)
}

func menu() error {
	in := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("\n========================================\n  Установщик 1C-Consultant\n========================================\n1. Установить или обновить\n2. Показать установленные версии\n3. Проверить обновления\n4. Откатить приложение\n5. Управление графами\n6. Установить или обновить Pi\n7. Удалить 1C-Consultant\n0. Выход\n\nВыберите действие: ")
		if !in.Scan() { return in.Err() }
		choice := strings.TrimSpace(in.Text())
		switch choice {
		case "0": return nil
		case "1":
			args, err := askSource(in); if err == nil { err = installCommand(args) }
			if err != nil { fmt.Println("Ошибка:", err) }
		case "2": if err := statusCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "3": if err := checkCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "4": if err := rollbackCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "5": if err := graphMenu(in); err != nil { fmt.Println("Ошибка:", err) }
		case "6": if err := installPiCommand(nil); err != nil { fmt.Println("Ошибка:", err) }
		case "7": if err := uninstallCommand(nil); err != nil { fmt.Println("Ошибка:", err) } else { return nil }
		default: fmt.Println("Неизвестный пункт")
		}
	}
}

func askSource(in *bufio.Scanner) ([]string, error) {
	fmt.Print("\nИсточник пакетов:\n1. Интернет — последний GitHub Release [по умолчанию]\n2. Скачанный offline bundle\n\nВыберите источник: ")
	if !in.Scan() { return nil, in.Err() }
	switch strings.TrimSpace(in.Text()) {
	case "", "1": return nil, nil
	case "2":
		fmt.Print("Путь к распакованному offline bundle: ")
		if !in.Scan() { return nil, in.Err() }
		path := strings.Trim(strings.TrimSpace(in.Text()), "\"")
		if path == "" { return nil, errors.New("путь не задан") }
		return []string{"--offline-path", path}, nil
	default: return nil, errors.New("неизвестный источник")
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
	if err := installInstaller(root, m, state, base, offline, log); err != nil { return err }
	state.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	if err := saveState(root, state); err != nil { return err }
	if state.ActiveApplication != "" { if err := writeLauncher(root, state); err != nil { return err }; if err := writeOSIntegration(root, state); err != nil { return err } }
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
	fmt.Printf("Installer: установлен %s, доступен %s\n", valueOr(s.ActiveInstaller, "нет"), m.Installer.Version)
	localPi := "нет"; if s.Pi != nil { localPi = s.Pi.Version }; fmt.Printf("Pi: установлен %s, доступен %s\n", localPi, m.Pi.Version)
	for _, g := range m.Graphs { local := "нет"; if x, ok := s.Graphs[g.ID]; ok { local = x.ActiveVersion }; fmt.Printf("Граф %s: установлен %s, доступен %s\n", g.ID, local, g.GraphVersion) }
	return nil
}

func statusCommand(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError); c := addCommon(fs, false)
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	s, err := loadState(root); if err != nil { return err }
	fmt.Println("Каталог:", root); fmt.Println("Активное приложение:", valueOr(s.ActiveApplication, "не установлено")); fmt.Println("Активный installer:", valueOr(s.ActiveInstaller, "не установлен")); if s.Pi != nil { fmt.Println("Pi:", s.Pi.Version, "Node.js:", s.Pi.NodeVersion) } else { fmt.Println("Pi: не установлен") }
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
	if err := saveState(root, s); err != nil { return err }; if err := writeLauncher(root, s); err != nil { return err }; if err := writeOSIntegration(root, s); err != nil { return err }
	log.info("Выполнен откат на " + s.ActiveApplication); return nil
}

func removeGraphCommand(args []string) error {
	fs := flag.NewFlagSet("remove-graph", flag.ContinueOnError); c := addCommon(fs, false); id := fs.String("graph", "", "ID графа")
	if err := fs.Parse(args); err != nil { return err }; if !safeSegment(*id) { return errors.New("некорректный ID графа") }
	root, err := dataDir(c.dataDir); if err != nil { return err }; s, err := loadState(root); if err != nil { return err }
	if _, ok := s.Graphs[*id]; !ok { return fmt.Errorf("граф %q не установлен", *id) }
	target := filepath.Join(root, "graphs", *id); if !within(filepath.Join(root, "graphs"), target) { return errors.New("путь графа выходит из каталога установки") }
	if err := os.RemoveAll(target); err != nil { return err }; delete(s.Graphs, *id); s.UpdatedAt = time.Now().UTC().Format(time.RFC3339); return saveState(root, s)
}

func rollbackGraphCommand(args []string) error {
	fs := flag.NewFlagSet("rollback-graph", flag.ContinueOnError); c := addCommon(fs, false); id := fs.String("graph", "", "ID графа")
	if err := fs.Parse(args); err != nil { return err }; if !safeSegment(*id) { return errors.New("некорректный ID графа") }
	root, err := dataDir(c.dataDir); if err != nil { return err }; s, err := loadState(root); if err != nil { return err }
	g, ok := s.Graphs[*id]; if !ok { return fmt.Errorf("граф %q не установлен", *id) }; if g.PreviousVersion == "" { return errors.New("предыдущая версия графа отсутствует") }
	previous, ok := g.Versions[g.PreviousVersion]; if !ok || !within(filepath.Join(root, "graphs"), previous.Path) { return errors.New("предыдущая версия графа отсутствует на диске") }
	g.ActiveVersion, g.PreviousVersion = g.PreviousVersion, g.ActiveVersion; g.Path = previous.Path; g.Installed = previous.Installed; s.Graphs[*id] = g; s.UpdatedAt = time.Now().UTC().Format(time.RFC3339); return saveState(root, s)
}

func graphMenu(in *bufio.Scanner) error {
	for {
		fmt.Print("\n--- Управление графами ---\n1. Показать установленные\n2. Скачать или обновить\n3. Откатить версию\n4. Удалить граф со всеми версиями\n0. Назад\n\nВыберите действие: ")
		if !in.Scan() { return in.Err() }
		switch strings.TrimSpace(in.Text()) {
		case "0": return nil
		case "1": if err := printInstalledGraphs(); err != nil { fmt.Println("Ошибка:", err) }
		case "2": if err := installGraphsFromMenu(in); err != nil { fmt.Println("Ошибка:", err) }
		case "3":
			id, err := chooseInstalledGraph(in); if err == nil { err = rollbackGraphCommand([]string{"--graph", id}) }; if err != nil { fmt.Println("Ошибка:", err) } else { fmt.Println("Предыдущая версия графа активирована") }
		case "4":
			id, err := chooseInstalledGraph(in); if err == nil { fmt.Print("Удалить граф и все его версии? [y/N]: "); if !in.Scan() { return in.Err() }; if !strings.EqualFold(strings.TrimSpace(in.Text()), "y") && !strings.EqualFold(strings.TrimSpace(in.Text()), "yes") { fmt.Println("Удаление отменено"); continue }; err = removeGraphCommand([]string{"--graph", id}) }; if err != nil { fmt.Println("Ошибка:", err) } else { fmt.Println("Граф удалён") }
		default: fmt.Println("Неизвестный пункт")
		}
	}
}

func printInstalledGraphs() error {
	root, err := dataDir(""); if err != nil { return err }; s, err := loadState(root); if err != nil { return err }
	ids := sortedGraphIDs(s); if len(ids) == 0 { fmt.Println("Графы не установлены"); return nil }
	for i, id := range ids { g := s.Graphs[id]; versions := make([]string, 0, len(g.Versions)); for version := range g.Versions { versions = append(versions, version) }; sort.Strings(versions); fmt.Printf("[%d] %s (%s)\n    Активна: %s; установлены: %s\n", i+1, g.Name, id, g.ActiveVersion, strings.Join(versions, ", ")) }
	return nil
}

func sortedGraphIDs(s *State) []string { ids := make([]string, 0, len(s.Graphs)); for id := range s.Graphs { ids = append(ids, id) }; sort.Strings(ids); return ids }

func chooseInstalledGraph(in *bufio.Scanner) (string, error) {
	root, err := dataDir(""); if err != nil { return "", err }; s, err := loadState(root); if err != nil { return "", err }; ids := sortedGraphIDs(s); if len(ids) == 0 { return "", errors.New("графы не установлены") }
	for i, id := range ids { g := s.Graphs[id]; fmt.Printf("[%d] %s — %s\n", i+1, g.Name, g.ActiveVersion) }; fmt.Print("Выберите номер: "); if !in.Scan() { return "", in.Err() }; var selected int; if _, err := fmt.Sscanf(strings.TrimSpace(in.Text()), "%d", &selected); err != nil || selected < 1 || selected > len(ids) { return "", errors.New("некорректный номер") }; return ids[selected-1], nil
}

func installGraphsFromMenu(in *bufio.Scanner) error {
	args, err := askSource(in); if err != nil { return err }; fs := flag.NewFlagSet("graph-install", flag.ContinueOnError); c := addCommon(fs, true); if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(""); if err != nil { return err }; log, err := openLog(root); if err != nil { return err }; defer log.close(); m, base, offline, err := loadManifest(*c, log); if err != nil { return err }; s, err := loadState(root); if err != nil { return err }; if s.ActiveApplication == "" { return errors.New("сначала установите приложение") }
	for i, g := range m.Graphs { installed := "не установлен"; if current, ok := s.Graphs[g.ID]; ok { installed = current.ActiveVersion }; fmt.Printf("[%d] %s %s — установлена %s, доступна %s\n", i+1, g.Name, g.ConfigurationVersion, installed, g.GraphVersion) }; fmt.Print("Введите номера через запятую или Enter для всех: "); if !in.Scan() { return in.Err() }
	selected := map[string]bool{}; answer := strings.TrimSpace(in.Text()); if answer == "" { for _, g := range m.Graphs { selected[g.ID] = true } } else { for item := range csvSet(answer) { var n int; if _, err := fmt.Sscanf(item, "%d", &n); err != nil || n < 1 || n > len(m.Graphs) { return fmt.Errorf("некорректный номер %q", item) }; selected[m.Graphs[n-1].ID] = true } }
	for id := range selected { g, _ := graphByID(m, id); if err := installGraph(root, g, s, base, offline, log); err != nil { return err } }; if err := installInstaller(root, m, s, base, offline, log); err != nil { return err }; s.UpdatedAt = time.Now().UTC().Format(time.RFC3339); if err := saveState(root, s); err != nil { return err }; return writeLauncher(root, s)
}

func installPiCommand(args []string) error {
	fs := flag.NewFlagSet("install-pi", flag.ContinueOnError); c := addCommon(fs, true)
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }; log, err := openLog(root); if err != nil { return err }; defer log.close()
	m, base, offline, err := loadManifest(*c, log); if err != nil { return err }; if offline { return errors.New("установка Pi требует подключения к интернету") }
	s, err := loadState(root); if err != nil { return err }; if s.ActiveApplication == "" { return errors.New("сначала установите 1C-Consultant") }
	if err := installPi(root, m, s, base, log); err != nil { return err }; s.UpdatedAt = time.Now().UTC().Format(time.RFC3339); if err := saveState(root, s); err != nil { return err }; if err := writeLauncher(root, s); err != nil { return err }
	log.info("Pi "+m.Pi.Version+" установлен. Запуск доступен из ярлыка 1C-Consultant."); return nil
}

func installPi(root string, m Manifest, s *State, base string, log *logger) error {
	osName, arch := platform(); var artifact *NodeArtifact
	for i := range m.Pi.NodeArtifacts { if m.Pi.NodeArtifacts[i].OS == osName && m.Pi.NodeArtifacts[i].Arch == arch { artifact = &m.Pi.NodeArtifacts[i]; break } }
	if artifact == nil { return fmt.Errorf("Pi %s/%s не поддерживается manifest", osName, arch) }
	nodeRoot := filepath.Join(root, "tools", "node", m.Pi.NodeVersion); node, err := safeJoin(nodeRoot, artifact.Node); if err != nil { return err }; npm, err := safeJoin(nodeRoot, artifact.NPM); if err != nil { return err }
	if _, err := os.Stat(node); errors.Is(err, os.ErrNotExist) { if err := fetchVerifyExtract(root, artifact.URL, base, false, "node", artifact.SHA256, artifact.Size, nodeRoot, log); err != nil { return err } }
	if !within(filepath.Join(root, "tools", "node"), node) || !within(filepath.Join(root, "tools", "node"), npm) { return errors.New("путь Node.js выходит из каталога установки") }
	piRoot := filepath.Join(root, "tools", "pi", m.Pi.Version); if err := os.MkdirAll(piRoot, 0o755); err != nil { return err }
	cmd := exec.Command(node, npm, "install", "--global", "--prefix", piRoot, "--ignore-scripts", "--no-audit", "--no-fund", m.Pi.Package+"@"+m.Pi.Version); cmd.Dir = root; cmd.Env = append(os.Environ(), "NPM_CONFIG_UPDATE_NOTIFIER=false")
	out, err := cmd.CombinedOutput(); if err != nil { return fmt.Errorf("npm install Pi: %w: %s", err, strings.TrimSpace(string(out))) }
	cli := filepath.Join(piRoot, "node_modules", "@earendil-works", "pi-coding-agent", "dist", "bundle", "cli.js"); if !within(filepath.Join(root, "tools", "pi"), cli) { return errors.New("путь Pi выходит из каталога установки") }
	health := exec.Command(node, cli, "--version"); health.Dir = root; healthOut, err := health.CombinedOutput(); if err != nil { return fmt.Errorf("проверка Pi: %w: %s", err, strings.TrimSpace(string(healthOut))) }
	s.Pi = &PiInstalled{Version:m.Pi.Version, NodeVersion:m.Pi.NodeVersion, NodeExecutable:node, CLI:cli, Installed:time.Now().UTC().Format(time.RFC3339)}
	return nil
}

func uninstallCommand(args []string) error {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError); c := addCommon(fs, false); yes := fs.Bool("yes", false, "подтвердить удаление")
	if err := fs.Parse(args); err != nil { return err }
	root, err := dataDir(c.dataDir); if err != nil { return err }
	if !within(filepath.Dir(root), root) || filepath.Clean(root) == filepath.Clean(filepath.VolumeName(root)+string(filepath.Separator)) { return errors.New("небезопасный каталог удаления") }
	if _, err := os.Stat(filepath.Join(root, "config", "installed.json")); err != nil { return errors.New("installed.json не найден; удаление отменено") }
	if !*yes { fmt.Printf("Удалить %s? [y/N]: ", root); var answer string; fmt.Scanln(&answer); if !strings.EqualFold(answer, "y") && !strings.EqualFold(answer, "yes") { return errors.New("удаление отменено") } }
	if err := removeOSIntegration(root); err != nil { return err }
	return removeInstallRoot(root)
}

func removeInstallRoot(root string) error {
	executable, _ := os.Executable()
	if runtime.GOOS == "windows" && within(root, executable) {
		script := "Start-Sleep -Milliseconds 800; Remove-Item -LiteralPath " + psQuote(root) + " -Recurse -Force"
		return exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encodePowerShell(script)).Start()
	}
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

func installInstaller(root string, m Manifest, s *State, base string, offline bool, log *logger) error {
	osName, arch := platform(); var artifact *InstallerArtifact
	for i := range m.Installer.Artifacts { if m.Installer.Artifacts[i].OS == osName && m.Installer.Artifacts[i].Arch == arch { artifact = &m.Installer.Artifacts[i]; break } }
	if artifact == nil { return fmt.Errorf("installer %s/%s не поддерживается manifest", osName, arch) }
	dest := filepath.Join(root, "installer", m.Installer.Version, artifact.Filename)
	if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) {
		if err := fetchVerifyFile(root, artifact.URL, base, offline, "installer", artifact.SHA256, artifact.Size, dest, log); err != nil { return err }
	}
	s.ActiveInstaller = m.Installer.Version
	s.Installers[m.Installer.Version] = Installed{Path: filepath.Dir(dest), Executable: artifact.Filename, Installed: time.Now().UTC().Format(time.RFC3339)}
	return nil
}

func installGraph(root string, g Graph, s *State, base string, offline bool, log *logger) error {
	if !safeSegment(g.ID) || !safeSegment(g.ConfigurationVersion) || !safeSegment(g.GraphVersion) { return errors.New("некорректный идентификатор графа") }
	active := s.ActiveApplication; if active == "" { return errors.New("сначала установите приложение") }
	if compareVersion(active, g.MinimumApplicationVersion) < 0 { return fmt.Errorf("граф %s требует приложение >= %s", g.ID, g.MinimumApplicationVersion) }
	dest := filepath.Join(root, "graphs", g.ID, g.ConfigurationVersion, g.GraphVersion)
	if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) { if err := fetchVerifyExtract(root, g.URL, base, offline, "graphs", g.SHA256, g.Size, dest, log); err != nil { return err } }
	now := time.Now().UTC().Format(time.RFC3339); current := s.Graphs[g.ID]
	if current.Versions == nil { current.Versions = map[string]GraphInstalled{} }
	if current.ActiveVersion != "" && current.Path != "" { current.Versions[current.ActiveVersion] = GraphInstalled{Path:current.Path, Installed:current.Installed} }
	if current.ActiveVersion != "" && current.ActiveVersion != g.GraphVersion { current.PreviousVersion = current.ActiveVersion }
	current.Name = g.Name; current.ConfigurationVersion = g.ConfigurationVersion; current.ActiveVersion = g.GraphVersion; current.Path = dest; current.Installed = now
	current.Versions[g.GraphVersion] = GraphInstalled{Path:dest, Installed:now}; s.Graphs[g.ID] = current
	return nil
}

func fetchVerifyFile(root, rawURL, base string, offline bool, kind, wantHash string, wantSize int64, dest string, log *logger) error {
	tmpDir := filepath.Join(root, "temp"); if err := os.MkdirAll(tmpDir, 0o755); err != nil { return err }
	tmp, err := os.CreateTemp(tmpDir, "download-*"); if err != nil { return err }; name := tmp.Name(); tmp.Close(); defer os.Remove(name)
	if err := fetch(rawURL, base, offline, kind, name); err != nil { return err }
	gotHash, size, err := fileHash(name); if err != nil { return err }
	if wantSize > 0 && size != wantSize { return fmt.Errorf("размер installer: ожидалось %d, получено %d", wantSize, size) }
	if !strings.EqualFold(gotHash, wantHash) { return fmt.Errorf("SHA-256 installer не совпал") }
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil { return err }
	if err := os.Chmod(name, 0o755); err != nil { return err }
	if err := renameWithRetry(name, dest); err != nil { return err }
	log.info("SHA-256 подтвержден для " + filepath.Base(rawURL)); return nil
}

func fetchVerifyExtract(root, rawURL, base string, offline bool, kind, wantHash string, wantSize int64, dest string, log *logger) error {
	tmpDir := filepath.Join(root, "temp"); if err := os.MkdirAll(tmpDir, 0o755); err != nil { return err }
	f, err := os.CreateTemp(tmpDir, "download-*"); if err != nil { return err }; archive := f.Name(); f.Close(); defer os.Remove(archive)
	if err := fetch(rawURL, base, offline, kind, archive); err != nil { return err }
	gotHash, size, err := fileHash(archive); if err != nil { return err }
	if wantSize > 0 && size != wantSize { return fmt.Errorf("размер пакета: ожидалось %d, получено %d", wantSize, size) }
	if !strings.EqualFold(gotHash, wantHash) { return fmt.Errorf("SHA-256 не совпал: ожидалось %s, получено %s", wantHash, gotHash) }
	log.info("SHA-256 подтвержден для " + filepath.Base(rawURL))
	destParent := filepath.Dir(dest); if err := os.MkdirAll(destParent, 0o755); err != nil { return err }
	stage, err := os.MkdirTemp(destParent, ".install-*"); if err != nil { return err }; defer os.RemoveAll(stage)
	if err := extract(archive, rawURL, stage); err != nil { return err }
	return renameWithRetry(stage, dest)
}

var renameFile = os.Rename

func renameWithRetry(source, target string) error {
	var err error
	for attempt := 0; attempt < 10; attempt++ {
		err = renameFile(source, target)
		if err == nil { return nil }
		if !errors.Is(err, os.ErrPermission) { return err }
		time.Sleep(time.Duration(attempt+1) * 50 * time.Millisecond)
	}
	return err
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
	for { h, err := tr.Next(); if errors.Is(err, io.EOF) { return nil }; if err != nil { return err }; target, err := safeJoin(dest, h.Name); if err != nil { return err }; switch h.Typeflag { case tar.TypeDir: if err := os.MkdirAll(target, 0o755); err != nil { return err }; case tar.TypeReg: if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil { return err }; out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(h.Mode).Perm()); if err != nil { return err }; _, copyErr := io.Copy(out, tr); closeErr := out.Close(); if copyErr != nil { return copyErr }; if closeErr != nil { return closeErr }; case tar.TypeSymlink, tar.TypeLink: continue; default: return fmt.Errorf("недопустимый тип элемента архива %q", h.Name) } }
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
	if !safeSegment(m.Installer.Version) { return errors.New("manifest: некорректная версия installer") }
	for _, a := range m.Installer.Artifacts { if a.OS == "" || a.Arch == "" || a.URL == "" || !safeSegment(a.Filename) || !validHash(a.SHA256) { return errors.New("manifest: неполный артефакт installer") } }
	if !safeSegment(m.Pi.Version) || !safeSegment(m.Pi.NodeVersion) || m.Pi.Package != "@earendil-works/pi-coding-agent" { return errors.New("manifest: некорректная версия Pi") }
	for _, a := range m.Pi.NodeArtifacts { if a.OS == "" || a.Arch == "" || a.URL == "" || a.Node == "" || a.NPM == "" || !validHash(a.SHA256) { return errors.New("manifest: неполный артефакт Node.js") }; if _, err := safeJoin("root", a.Node); err != nil { return errors.New("manifest: небезопасный путь Node.js") }; if _, err := safeJoin("root", a.NPM); err != nil { return errors.New("manifest: небезопасный путь npm") } }
	seen := map[string]bool{}; for _, g := range m.Graphs { if !safeSegment(g.ID) || seen[g.ID] || g.URL == "" || !validHash(g.SHA256) { return fmt.Errorf("manifest: некорректный граф %q", g.ID) }; seen[g.ID] = true }
	return nil
}

func loadState(root string) (*State, error) {
	s := &State{SchemaVersion:1, Applications:map[string]Installed{}, Installers:map[string]Installed{}, Graphs:map[string]GraphState{}}
	data, err := os.ReadFile(filepath.Join(root, "config", "installed.json")); if errors.Is(err, os.ErrNotExist) { return s, nil }; if err != nil { return nil, err }; if err := json.Unmarshal(data, s); err != nil { return nil, err }
	if s.Applications == nil { s.Applications = map[string]Installed{} }; if s.Installers == nil { s.Installers = map[string]Installed{} }; if s.Graphs == nil { s.Graphs = map[string]GraphState{} }
	for id, graph := range s.Graphs { if graph.Versions == nil { graph.Versions = map[string]GraphInstalled{} }; if graph.ActiveVersion != "" && graph.Path != "" { graph.Versions[graph.ActiveVersion] = GraphInstalled{Path:graph.Path, Installed:graph.Installed} }; s.Graphs[id] = graph }
	return s, nil
}

func saveState(root string, s *State) error {
	dir := filepath.Join(root, "config"); if err := os.MkdirAll(dir, 0o755); err != nil { return err }; data, err := json.MarshalIndent(s, "", "  "); if err != nil { return err }; tmp, err := os.CreateTemp(dir, "installed-*.tmp"); if err != nil { return err }; name := tmp.Name(); defer os.Remove(name); if err := tmp.Chmod(0o600); err != nil { tmp.Close(); return err }; if _, err := tmp.Write(append(data, '\n')); err != nil { tmp.Close(); return err }; if err := tmp.Close(); err != nil { return err }
	target, backup := filepath.Join(dir, "installed.json"), filepath.Join(dir, "installed.json.bak"); _ = os.Remove(backup)
	if err := renameWithRetry(target, backup); err != nil && !errors.Is(err, os.ErrNotExist) { return err }
	if err := renameWithRetry(name, target); err != nil { _ = renameWithRetry(backup, target); return err }
	_ = os.Remove(backup); return nil
}

func writeLauncher(root string, s *State) error {
	a, ok := s.Applications[s.ActiveApplication]; if !ok { return errors.New("активное приложение отсутствует") }; exe, err := safeJoin(a.Path, a.Executable); if err != nil { return err }
	i, ok := s.Installers[s.ActiveInstaller]; if !ok { return errors.New("активный installer отсутствует") }; installer, err := safeJoin(i.Path, i.Executable); if err != nil { return err }
	if !within(filepath.Join(root, "app"), a.Path) { return errors.New("путь приложения выходит из каталога установки") }
	if !within(filepath.Join(root, "installer"), i.Path) { return errors.New("путь installer выходит из каталога установки") }
	if runtime.GOOS == "windows" { piMenu, piAction := "", ""; if s.Pi != nil { piMenu = "echo 3. Запустить Pi для 1C-Consultant\r\n"; piAction = "if \"%choice%\"==\"3\" (call \""+filepath.Join(root, "1C-Consultant-Pi.cmd")+"\" & goto end)\r\n" }; body := "@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\nset \"CONSULTANT_DATA_DIR="+root+"\"\r\nset \"CONSULTANT_INSTALL_ROOT="+root+"\"\r\nif not \"%~1\"==\"\" goto service\r\necho.\r\necho 1. Запустить 1C-Consultant\r\necho 2. Управление установкой и графами\r\n"+piMenu+"echo 0. Выход\r\nset /p choice=Выберите действие: \r\nif \"%choice%\"==\"2\" (\""+installer+"\" & goto end)\r\n"+piAction+"if \"%choice%\"==\"0\" goto end\r\n:service\r\ncd /d \""+a.Path+"\"\r\n\""+exe+"\" %*\r\n:end\r\nendlocal\r\n"; if err := os.WriteFile(filepath.Join(root, "1C-Consultant.cmd"), []byte(body), 0o755); err != nil { return err } } else {
		piMenu, piAction := "", ""; if s.Pi != nil { piMenu = "3. Запустить Pi для 1C-Consultant\\n"; piAction = "    3) exec "+shellQuote(filepath.Join(root, "1c-consultant-pi"))+" ;;\n" }
		body := "#!/bin/sh\nexport CONSULTANT_DATA_DIR="+shellQuote(root)+"\nexport CONSULTANT_INSTALL_ROOT="+shellQuote(root)+"\nif [ \"$#\" -eq 0 ]; then\n  printf '\\n1. Запустить 1C-Consultant\\n2. Управление установкой и графами\\n"+piMenu+"0. Выход\\nВыберите действие: '\n  read -r choice\n  case \"$choice\" in\n    2) exec "+shellQuote(installer)+" ;;\n"+piAction+"    0) exit 0 ;;\n  esac\nfi\ncd "+shellQuote(a.Path)+"\nexec "+shellQuote(exe)+" \"$@\"\n"
		if err := os.WriteFile(filepath.Join(root, "1c-consultant"), []byte(body), 0o755); err != nil { return err }; if runtime.GOOS == "darwin" { if err := os.WriteFile(filepath.Join(root, "1C-Consultant.command"), []byte(body), 0o755); err != nil { return err } }
	}
	if err := writeAgentWorkspace(root, exe, a.Path); err != nil { return err }
	if s.Pi != nil { return writePiLauncher(root, s, exe, a.Path) }; return nil
}

func writePiLauncher(root string, s *State, appExecutable, appPath string) error {
	if s.Pi == nil { return errors.New("Pi не установлен") }; if !within(filepath.Join(root, "tools", "node"), s.Pi.NodeExecutable) || !within(filepath.Join(root, "tools", "pi"), s.Pi.CLI) { return errors.New("пути Pi выходят из каталога установки") }
	if runtime.GOOS == "windows" { body := "@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\nset \"CONSULTANT_DATA_DIR="+root+"\"\r\nset \"CONSULTANT_INSTALL_ROOT="+root+"\"\r\nset \"CONSULTANT_REPO="+appPath+"\"\r\nset \"CONSULTANT_EXECUTABLE="+appExecutable+"\"\r\ncd /d \""+appPath+"\"\r\n\""+s.Pi.NodeExecutable+"\" \""+s.Pi.CLI+"\" --approve --offline --model wormsoft-gateway/wormsoft/agent/medium --name \"NewAgent ERP\"\r\nendlocal\r\n"; return os.WriteFile(filepath.Join(root, "1C-Consultant-Pi.cmd"), []byte(body), 0o755) }
	body := "#!/bin/sh\nexport CONSULTANT_DATA_DIR="+shellQuote(root)+"\nexport CONSULTANT_INSTALL_ROOT="+shellQuote(root)+"\nexport CONSULTANT_REPO="+shellQuote(appPath)+"\nexport CONSULTANT_EXECUTABLE="+shellQuote(appExecutable)+"\ncd "+shellQuote(appPath)+"\nexec "+shellQuote(s.Pi.NodeExecutable)+" "+shellQuote(s.Pi.CLI)+" --approve --offline --model wormsoft-gateway/wormsoft/agent/medium --name 'NewAgent ERP'\n"; return os.WriteFile(filepath.Join(root, "1c-consultant-pi"), []byte(body), 0o755)
}

func writeAgentWorkspace(root, appExecutable, appPath string) error {
	instructions, err := os.ReadFile(filepath.Join(appPath, "AGENTS.md")); if err != nil { return fmt.Errorf("инструкции внешних интерфейсов: %w", err) }
	instructions = append(instructions, []byte("\n## Установленная версия\n\nРабочий каталог содержит установленный 1C-Consultant. На Windows вызывай `consultant.cmd`, на macOS/Linux — `./consultant`. Активный проект записан в `config/selected-project.txt`. Не изменяй файлы в `app/`, `graphs/`, `installer/` и `tools/`.\n")...)
	if err := os.WriteFile(filepath.Join(root, "AGENTS.md"), instructions, 0o644); err != nil { return err }
	if runtime.GOOS == "windows" {
		body := "@echo off\r\nchcp 65001 >nul\r\nset \"CONSULTANT_DATA_DIR="+root+"\"\r\nset \"CONSULTANT_REPO="+appPath+"\"\r\ncd /d \""+appPath+"\"\r\n\""+appExecutable+"\" --repo \""+appPath+"\" %*\r\n"
		return os.WriteFile(filepath.Join(root, "consultant.cmd"), []byte(body), 0o755)
	}
	body := "#!/bin/sh\nexport CONSULTANT_DATA_DIR="+shellQuote(root)+"\nexport CONSULTANT_REPO="+shellQuote(appPath)+"\ncd "+shellQuote(appPath)+"\nexec "+shellQuote(appExecutable)+" --repo "+shellQuote(appPath)+" \"$@\"\n"
	return os.WriteFile(filepath.Join(root, "consultant"), []byte(body), 0o755)
}

func writeOSIntegration(root string, s *State) error {
	if runtime.GOOS == "darwin" && externalAppManaged() { return nil }
	switch runtime.GOOS { case "windows": if err := copyProgramIcon(root, s, "1c-consultant.ico"); err != nil { return err }; return runPowerShell(windowsShortcutScript(root, false)); case "darwin": home, err := os.UserHomeDir(); if err != nil { return err }; return writeMacOSAppAt(home, root, s.ActiveApplication); default: return nil }
}

func removeOSIntegration(root string) error {
	if runtime.GOOS == "darwin" && externalAppManaged() { return nil }
	switch runtime.GOOS { case "windows": return runPowerShell(windowsShortcutScript(root, true)); case "darwin": home, err := os.UserHomeDir(); if err != nil { return err }; return removeMacOSAppAt(home); default: return nil }
}

func externalAppManaged() bool { return os.Getenv("CONSULTANT_EXTERNAL_APP") == "1" }

func windowsShortcutScript(root string, remove bool) string {
	prefix := "$ErrorActionPreference='Stop';$desktop=[Environment]::GetFolderPath('Desktop');$programs=[Environment]::GetFolderPath('Programs');$menu=Join-Path $programs '1C-Consultant';$desktopLink=Join-Path $desktop '1C-Consultant.lnk';$menuLink=Join-Path $menu '1C-Consultant.lnk';"
	if remove { return prefix+"Remove-Item -LiteralPath $desktopLink,$menuLink -Force -ErrorAction SilentlyContinue;if((Test-Path -LiteralPath $menu)-and-not(Get-ChildItem -LiteralPath $menu -Force)){Remove-Item -LiteralPath $menu -Force}" }
	return prefix+"$ws=New-Object -ComObject WScript.Shell;[IO.Directory]::CreateDirectory($menu)|Out-Null;$target="+psQuote(filepath.Join(root, "1C-Consultant.cmd"))+";$working="+psQuote(root)+";$icon="+psQuote(filepath.Join(root, "config", "1c-consultant.ico"))+";foreach($path in @($desktopLink,$menuLink)){$shortcut=$ws.CreateShortcut($path);$shortcut.TargetPath=$target;$shortcut.WorkingDirectory=$working;$shortcut.IconLocation=$icon;$shortcut.Description='1C-Consultant';$shortcut.Save()}"
}

func copyProgramIcon(root string, s *State, name string) error {
	a, ok := s.Applications[s.ActiveApplication]; if !ok { return errors.New("активное приложение отсутствует") }
	source, err := safeJoin(a.Path, filepath.Join("assets", name)); if err != nil { return err }; data, err := os.ReadFile(source); if errors.Is(err, os.ErrNotExist) { return nil }; if err != nil { return fmt.Errorf("иконка программы: %w", err) }
	dest := filepath.Join(root, "config", name); if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil { return err }; return os.WriteFile(dest, data, 0o644)
}

func runPowerShell(script string) error {
	out, err := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encodePowerShell(script)).CombinedOutput()
	if err != nil { return fmt.Errorf("не удалось обновить ярлыки Windows: %w: %s", err, strings.TrimSpace(string(out))) }; return nil
}

func encodePowerShell(script string) string {
	words := utf16.Encode([]rune(script)); data := make([]byte, len(words)*2); for i, word := range words { data[i*2] = byte(word); data[i*2+1] = byte(word >> 8) }; return base64.StdEncoding.EncodeToString(data)
}

func writeMacOSAppAt(home, root, version string) error {
	contents := filepath.Join(home, "Applications", "1C-Consultant.app", "Contents"); macOSDir := filepath.Join(contents, "MacOS"); resources := filepath.Join(contents, "Resources")
	if err := os.MkdirAll(macOSDir, 0o755); err != nil { return err }; if err := os.MkdirAll(resources, 0o755); err != nil { return err }
	icon, err := os.ReadFile(filepath.Join(root, "app", version, "assets", "1c-consultant.icns")); if err == nil { if err := os.WriteFile(filepath.Join(resources, "1C-Consultant.icns"), icon, 0o644); err != nil { return err } } else if !errors.Is(err, os.ErrNotExist) { return fmt.Errorf("иконка программы: %w", err) }
	plist := "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict>\n<key>CFBundleDisplayName</key><string>1C-Consultant</string>\n<key>CFBundleExecutable</key><string>1C-Consultant</string>\n<key>CFBundleIconFile</key><string>1C-Consultant.icns</string>\n<key>CFBundleIdentifier</key><string>com.ilnurcode.1c-consultant</string>\n<key>CFBundleName</key><string>1C-Consultant</string>\n<key>CFBundlePackageType</key><string>APPL</string>\n<key>CFBundleShortVersionString</key><string>"+html.EscapeString(version)+"</string>\n</dict></plist>\n"
	if err := os.WriteFile(filepath.Join(contents, "Info.plist"), []byte(plist), 0o644); err != nil { return err }
	body := "#!/bin/sh\nexec /usr/bin/open -a Terminal "+shellQuote(filepath.Join(root, "1C-Consultant.command"))+"\n"; return os.WriteFile(filepath.Join(macOSDir, "1C-Consultant"), []byte(body), 0o755)
}

func removeMacOSAppAt(home string) error {
	app := filepath.Join(home, "Applications", "1C-Consultant.app"); data, err := os.ReadFile(filepath.Join(app, "Contents", "Info.plist"))
	if errors.Is(err, os.ErrNotExist) { return nil }; if err != nil { return err }; if !strings.Contains(string(data), "com.ilnurcode.1c-consultant") { return errors.New("приложение ~/Applications/1C-Consultant.app не принадлежит установщику") }; return os.RemoveAll(app)
}

func psQuote(value string) string { return "'"+strings.ReplaceAll(value, "'", "''")+"'" }
func shellQuote(value string) string { return "'"+strings.ReplaceAll(value, "'", "'\\''")+"'" }

func dataDir(override string) (string, error) {
	if override != "" { return filepath.Abs(override) }
	if managed := os.Getenv("CONSULTANT_INSTALL_ROOT"); managed != "" { return filepath.Abs(managed) }
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
