package main

// Phase 2 — deploy/enforce. Windows-specific operations: sparse git checkout of
// the deployed ref, robocopy mirror of the repo worktree onto live locations
// (honoring excludes so junk like Navdata is neither copied nor deleted), and
// app restart in start_delay order. These shell out to git/robocopy/taskkill, so
// they compile on any OS but only run meaningfully on Windows.

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

var gitMu sync.Mutex
var gitResolved string

// gitExe returns the git executable. Explicit cfg.GitExe wins; otherwise we look on
// PATH, then probe the standard Git-for-Windows install locations — a Windows
// service often runs with a trimmed PATH that lacks git even when it's installed.
// A successful result is cached; a miss is NOT, so installing git later is picked up
// on the next deploy without restarting the agent.
func gitExe(cfg AgentConfig) string {
	if cfg.GitExe != "" {
		return cfg.GitExe
	}
	gitMu.Lock()
	defer gitMu.Unlock()
	if gitResolved != "" {
		return gitResolved
	}
	if p, err := exec.LookPath("git"); err == nil {
		log.Printf("[git] resolved via PATH: %s", p)
		gitResolved = p
		return gitResolved
	}
	for _, c := range []string{
		`C:\Program Files\Git\cmd\git.exe`,
		`C:\Program Files\Git\bin\git.exe`,
		`C:\Program Files (x86)\Git\cmd\git.exe`,
		filepath.Join(os.Getenv("LOCALAPPDATA"), `Programs\Git\cmd\git.exe`),
		filepath.Join(os.Getenv("ProgramFiles"), `Git\cmd\git.exe`),
	} {
		if c != "" {
			if _, err := os.Stat(c); err == nil {
				gitResolved = c
				log.Printf("[git] using %s (not on PATH)", c)
				return gitResolved
			}
		}
	}
	// Portable git installed via the Installs panel — search the tree since the
	// bundle's exact layout can vary (cmd\git.exe vs mingw64\bin\git.exe, nested).
	p := findGitIn(`C:\sim-agent\git`)
	log.Printf("[git] findGitIn(C:\\sim-agent\\git) = %q", p)
	if p != "" {
		gitResolved = p
		log.Printf("[git] using %s (portable install)", p)
		return gitResolved
	}
	log.Printf("[git] NOT FOUND — %s", installDirInfo())
	return "git" // not found yet — don't cache; re-probe next deploy
}

// installDirInfo reports what's under the portable-git install dir, so a failed
// deploy self-explains in the dashboard whether the bundle actually unzipped there.
func installDirInfo() string {
	dir := `C:\sim-agent\git`
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Sprintf("%s: %v", dir, err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	git := findGitIn(dir)
	if git == "" {
		git = "no git.exe found under it"
	}
	return fmt.Sprintf("%s contains %v; %s", dir, names, git)
}

// findGitIn walks a directory for git.exe, preferring a cmd\git.exe wrapper, then
// any git.exe (e.g. mingw64\bin\git.exe). Empty if none.
func findGitIn(dir string) string {
	if _, err := os.Stat(dir); err != nil {
		return ""
	}
	cmdHit, anyHit := "", ""
	_ = filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err != nil || info == nil || info.IsDir() {
			return nil // tolerate unreadable subdirs; keep walking
		}
		if strings.EqualFold(info.Name(), "git.exe") {
			if cmdHit == "" && strings.Contains(strings.ToLower(p), `\cmd\`) {
				cmdHit = p // the cmd\git.exe wrapper is preferred
			} else if anyHit == "" {
				anyHit = p
			}
		}
		return nil
	})
	if cmdHit != "" {
		return cmdHit
	}
	return anyHit
}

// gitCmd runs git inside the local clone (cfg.RepoPath).
func gitCmd(cfg AgentConfig, args ...string) (string, error) {
	full := append([]string{"-C", cfg.RepoPath}, args...)
	out, err := exec.Command(gitExe(cfg), full...).CombinedOutput()
	return string(out), err
}

// GitFetchCheckout ensures a sparse clone exists, fetches, and checks out `ref`
// (a moving branch like training-live/dev, or an immutable tag). Only `folder`
// is materialised (sparse cone), so each PC pulls only what it runs.
func GitFetchCheckout(cfg AgentConfig, folder, ref string) error {
	if out, err := exec.Command(gitExe(cfg), "--version").CombinedOutput(); err != nil {
		return fmt.Errorf("git not found on this PC — install Git for Windows (deploy needs it): %v: %s [%s]", err, out, installDirInfo())
	}
	if _, err := os.Stat(filepath.Join(cfg.RepoPath, ".git")); err != nil {
		if cfg.GitRemote == "" {
			return fmt.Errorf("no git_remote configured in agent.json; cannot deploy")
		}
		if err := os.MkdirAll(filepath.Dir(cfg.RepoPath), 0o755); err != nil {
			return err
		}
		out, err := exec.Command(gitExe(cfg), "clone", "--no-checkout", cfg.GitRemote, cfg.RepoPath).CombinedOutput()
		if err != nil {
			return fmt.Errorf("clone failed: %v: %s", err, out)
		}
		if out, err := gitCmd(cfg, "sparse-checkout", "init", "--cone"); err != nil {
			return fmt.Errorf("sparse-checkout init: %v: %s", err, out)
		}
	}
	// Configure Git LFS filters for this clone so large binaries materialize on
	// checkout. Non-fatal: a PC without git-lfs / a config-only load still works.
	if out, err := gitCmd(cfg, "lfs", "install", "--local"); err != nil {
		log.Printf("[deploy] lfs install: %v: %s", err, out)
	}
	if out, err := gitCmd(cfg, "sparse-checkout", "set", folder); err != nil {
		return fmt.Errorf("sparse-checkout set: %v: %s", err, out)
	}
	// --force so a recreated tag (e.g. v1.0 after a reset/re-seal) doesn't get
	// rejected as "would clobber existing tag" and abort the whole deploy.
	if out, err := gitCmd(cfg, "fetch", "--prune", "--tags", "--force", "origin"); err != nil {
		return fmt.Errorf("fetch: %v: %s", err, out)
	}
	// Resolve to the target object: a branch (training-live/dev) tracks its remote
	// tip; otherwise it's a tag/sha.
	target := ref
	if _, err := gitCmd(cfg, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/"+ref); err == nil {
		target = "refs/remotes/origin/" + ref
		if out, err := gitCmd(cfg, "checkout", "-f", "-B", ref, target); err != nil {
			return fmt.Errorf("checkout branch %s: %v: %s", ref, err, out)
		}
	} else if out, err := gitCmd(cfg, "checkout", "-f", ref); err != nil {
		return fmt.Errorf("checkout ref %s: %v: %s", ref, err, out)
	}
	// Force the worktree to match the ref EXACTLY: discard tracked edits AND any
	// untracked leftovers (e.g. files a prior capture wrote into the sparse
	// worktree). git checkout alone never removes untracked files, so without this
	// deploy could not guarantee determinism — stale files would be mirrored to live.
	if out, err := gitCmd(cfg, "reset", "--hard", target); err != nil {
		return fmt.Errorf("reset --hard %s: %v: %s", target, err, out)
	}
	if out, err := gitCmd(cfg, "clean", "-fd", "--", folder); err != nil {
		return fmt.Errorf("clean: %v: %s", err, out)
	}
	// Materialize LFS blobs for the checked-out ref (real binaries, not pointers).
	// Non-fatal + loud: if this PC versions binaries it MUST have git-lfs installed
	// (Git for Windows bundles it) or the mirror would push pointer files to live.
	if out, err := gitCmd(cfg, "lfs", "pull"); err != nil {
		log.Printf("[deploy] WARN lfs pull failed — install git-lfs if this PC versions binaries: %v: %s", err, out)
	}
	return nil
}

// robocopyExcludes maps our /-separated **-globs onto robocopy /XD (dir names)
// and /XF (file specs). Approximate but matches the denylist intent: dir globs
// like "**/Navdata/**" -> /XD Navdata; file globs like "**/*.log" -> /XF *.log.
// Crucially, excluded dirs/files are neither copied NOR deleted by /MIR, so live
// Navdata/logs survive a deploy.
func robocopyExcludes(patterns []string) (xd, xf []string) {
	seenD, seenF := map[string]bool{}, map[string]bool{}
	for _, p := range patterns {
		p = strings.TrimSpace(strings.ReplaceAll(p, "\\", "/"))
		if p == "" {
			continue
		}
		if strings.HasSuffix(p, "/**") {
			dir := strings.TrimPrefix(strings.TrimSuffix(p, "/**"), "**/")
			if i := strings.LastIndex(dir, "/"); i >= 0 {
				dir = dir[i+1:]
			}
			if dir != "" && dir != "**" && !seenD[dir] {
				seenD[dir] = true
				xd = append(xd, dir)
			}
			continue
		}
		base := p
		if i := strings.LastIndex(base, "/"); i >= 0 {
			base = base[i+1:]
		}
		if base != "" && !strings.Contains(base, "**") && !seenF[base] {
			seenF[base] = true
			xf = append(xf, base)
		}
	}
	return
}

// MirrorToLive mirrors each app's repo subtree onto its live dir(s) with /MIR
// (delete extraneous), protecting excluded paths. Repo subtree layout matches
// what import produced: <repo>/<label>, where label is "" for single-live apps.
func MirrorToLive(cfg AgentConfig, apps map[string]AppSpec) error {
	for name, app := range apps {
		if app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		xd, xf := robocopyExcludes(app.Exclude)
		labels := liveLabels(app.Live)
		for _, live := range app.Live {
			src := filepath.Join(cfg.RepoPath, filepath.FromSlash(app.Repo))
			if lbl := labels[live]; lbl != "" {
				src = filepath.Join(src, lbl)
			}
			if _, err := os.Stat(src); err != nil {
				log.Printf("[mirror] %s: repo subtree %s missing, skipping", name, src)
				continue
			}
			dst := filepath.FromSlash(live)
			if err := robocopyMirror(src, dst, xd, xf); err != nil {
				return fmt.Errorf("mirror %s -> %s: %w", name, dst, err)
			}
			log.Printf("[mirror] %s: %s -> %s", name, src, dst)
		}
	}
	return nil
}

func robocopyMirror(src, dst string, xd, xf []string) error {
	args := []string{src, dst, "/MIR", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP"}
	for _, d := range xd {
		args = append(args, "/XD", d)
	}
	if len(xf) > 0 {
		args = append(args, "/XF")
		args = append(args, xf...)
	}
	cmd := exec.Command("robocopy", args...)
	out, _ := cmd.CombinedOutput() // robocopy returns 1 on success-with-copies
	if cmd.ProcessState == nil {
		return fmt.Errorf("robocopy did not run (Windows only): %s", out)
	}
	code := cmd.ProcessState.ExitCode()
	// robocopy exit bits: >=16 = fatal (abort). Bit 8 = some files couldn't be copied
	// (locked / in-use by a running app) — robocopy already copied everything else, so
	// we log and keep going instead of failing the whole deploy on a mapped .db-shm etc.
	if code >= 16 {
		return fmt.Errorf("robocopy fatal exit %d: %s", code, string(out))
	}
	if code&8 != 0 {
		log.Printf("[mirror] WARN %s: some files are in use and were skipped (exit %d) — close the app to sync them", dst, code)
	}
	return nil
}

// repoCloned reports whether this PC has ever pulled the repo (so there's a
// deployed worktree to adopt on startup).
func repoCloned(cfg AgentConfig) bool {
	_, err := os.Stat(filepath.Join(cfg.RepoPath, ".git"))
	return err == nil
}

// appsRunning reports whether every launchable app's exe is currently running, so
// startup can tell "agent restarted while the sim is up" (adopt, don't redeploy)
// from "cold boot, sim is down" (deploy + launch).
func appsRunning(apps map[string]AppSpec) bool {
	exes := appExes(apps)
	if len(exes) == 0 {
		return false
	}
	out, _ := exec.Command("tasklist", "/FO", "CSV", "/NH").CombinedOutput()
	running := strings.ToLower(string(out))
	for exe := range exes {
		if !strings.Contains(running, strings.ToLower(exe)) {
			return false
		}
	}
	return true
}

// appExes returns the distinct exe image names of the versioned, launchable apps.
func appExes(apps map[string]AppSpec) map[string]bool {
	exes := map[string]bool{}
	for _, app := range apps {
		if app.Run == "" || app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		exes[filepath.Base(filepath.FromSlash(app.Run))] = true
	}
	return exes
}

// StopApps gracefully stops the versioned apps (close request, then force-kill
// stragglers) so their file handles are released BEFORE the worktree is mirrored
// onto live. Apps without a repo/run (e.g. ImmersiveDisplayPRO) are left alone.
// Called before the mirror so nothing runs against half-synced files.
func StopApps(apps map[string]AppSpec) {
	exes := appExes(apps)
	if len(exes) == 0 {
		return
	}
	for exe := range exes {
		_ = exec.Command("taskkill", "/IM", exe).Run() // graceful close
	}
	time.Sleep(4 * time.Second)
	for exe := range exes {
		_ = exec.Command("taskkill", "/F", "/IM", exe).Run() // force stragglers
	}
}

// StartApps launches the versioned apps in start_delay order — ONLY after the
// sync has completed (the caller mirrors first). This is the guarantee that no
// app runs before its files are fully in place.
func StartApps(apps map[string]AppSpec) {
	type item struct {
		name string
		spec AppSpec
	}
	var items []item
	for name, app := range apps {
		if app.Run == "" || app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		items = append(items, item{name, app})
	}
	sort.Slice(items, func(i, j int) bool { return items[i].spec.StartDelay < items[j].spec.StartDelay })
	start := time.Now()
	for _, it := range items {
		target := time.Duration(it.spec.StartDelay) * time.Second
		if d := target - time.Since(start); d > 0 {
			time.Sleep(d)
		}
		run := filepath.FromSlash(it.spec.Run)
		cmd := exec.Command(run)
		cmd.Dir = filepath.Dir(run)
		if err := cmd.Start(); err != nil {
			log.Printf("[launch] %s failed: %v", it.name, err)
			continue
		}
		log.Printf("[launch] %s (delay %ds)", it.name, it.spec.StartDelay)
	}
}

// CheckClean reports whether live matches the deployed ref. It uses the SAME
// file-level comparison as the "diff" view (DriftFiles), so clean/dirty and the
// diff can never disagree: dirty ⟺ at least one FILE differs. Directory-only or
// timestamp quirks (which robocopy's exit code would flag) don't count as drift,
// since this system versions files, not empty dirs.
func CheckClean(cfg AgentConfig, apps map[string]AppSpec) bool {
	return len(DriftFiles(cfg, apps)) == 0
}

// DriftEntry is one file that differs between the deployed version (repo worktree)
// and the live directory. Kind: "new" = in the version, missing from live;
// "changed" = present in both but different; "extra" = in live, not in the version.
type DriftEntry struct {
	App  string `json:"app"`
	Kind string `json:"kind"`
	Path string `json:"path"`
}

var driftLineRe = regexp.MustCompile(`(?i)^\s*(\*EXTRA File|New File|Newer|Older|Changed)\s+\d+\s+(.+?)\s*$`)

// DriftFiles lists exactly which files differ between the deployed version and live
// (a robocopy /L dry-run, parsed), so an operator can see WHAT made a PC "dirty".
func DriftFiles(cfg AgentConfig, apps map[string]AppSpec) []DriftEntry {
	var out []DriftEntry
	for name, app := range apps {
		if app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		xd, xf := robocopyExcludes(app.Exclude)
		labels := liveLabels(app.Live)
		for _, live := range app.Live {
			src := filepath.Join(cfg.RepoPath, filepath.FromSlash(app.Repo))
			if lbl := labels[live]; lbl != "" {
				src = filepath.Join(src, lbl)
			}
			if _, err := os.Stat(src); err != nil {
				continue
			}
			out = append(out, driftDiff(src, filepath.FromSlash(live), xd, xf, name)...)
			if len(out) >= 1000 {
				return out[:1000] // cap payload; a huge drift means "resync", not "read list"
			}
		}
	}
	return out
}

func driftDiff(src, dst string, xd, xf []string, app string) []DriftEntry {
	args := []string{src, dst, "/MIR", "/L", "/NDL", "/NJH", "/NJS", "/NP", "/FP", "/BYTES", "/R:0", "/W:0"}
	for _, d := range xd {
		args = append(args, "/XD", d)
	}
	if len(xf) > 0 {
		args = append(args, "/XF")
		args = append(args, xf...)
	}
	out, _ := exec.Command("robocopy", args...).CombinedOutput()
	var entries []DriftEntry
	for _, line := range strings.Split(string(out), "\n") {
		m := driftLineRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		marker, path := m[1], strings.TrimSpace(m[2])
		kind := "changed"
		if strings.EqualFold(marker, "New File") {
			kind = "new"
		} else if strings.EqualFold(marker, "*EXTRA File") {
			kind = "extra"
		}
		rel := path
		for _, base := range []string{src, dst} {
			if len(rel) >= len(base) && strings.EqualFold(rel[:len(base)], base) {
				rel = strings.TrimLeft(rel[len(base):], `\/`)
				break
			}
		}
		entries = append(entries, DriftEntry{App: app, Kind: kind, Path: rel})
	}
	return entries
}
