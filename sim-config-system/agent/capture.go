package main

// Phase 3 — dev capture. Reverse of deploy: gracefully stop the apps (so their
// config files aren't mid-write), mirror live -> the sparse worktree (honoring
// excludes), then compute what changed vs the dev branch and upload a bundle.
// The agent never commits — the coordinator applies the bundle to dev under the
// dev-session lock. Reuses deploy.go helpers (gitCmd, robocopy*, liveLabels).

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// QuiesceApps gracefully stops versioned apps (taskkill without /F sends a close
// to GUI windows), waits for them to flush, then force-kills stragglers so all
// file handles are released before we snapshot live.
func QuiesceApps(apps map[string]AppSpec) {
	exes := map[string]bool{}
	for _, app := range apps {
		if app.Run == "" || app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		exes[filepath.Base(filepath.FromSlash(app.Run))] = true
	}
	for exe := range exes {
		_ = exec.Command("taskkill", "/IM", exe).Run() // graceful
	}
	time.Sleep(4 * time.Second)
	for exe := range exes {
		_ = exec.Command("taskkill", "/F", "/IM", exe).Run() // force stragglers
	}
}

// MirrorToWorktree mirrors each app's live dir(s) onto its repo subtree in the
// sparse clone, so `git status` then reflects exactly what changed vs dev.
func MirrorToWorktree(cfg AgentConfig, apps map[string]AppSpec) error {
	for name, app := range apps {
		if app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		xd, xf := robocopyExcludes(app.Exclude)
		labels := liveLabels(app.Live)
		for _, live := range app.Live {
			dst := filepath.Join(cfg.RepoPath, filepath.FromSlash(app.Repo))
			if lbl := labels[live]; lbl != "" {
				dst = filepath.Join(dst, lbl)
			}
			src := filepath.FromSlash(live)
			if _, err := os.Stat(src); err != nil {
				log.Printf("[capture] %s: live %s missing, skipping", name, src)
				continue
			}
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return err
			}
			if err := robocopyMirror(src, dst, xd, xf); err != nil {
				return fmt.Errorf("mirror %s live->worktree: %w", name, err)
			}
			log.Printf("[capture] mirror %s: %s -> %s", name, src, dst)
		}
	}
	return nil
}

// ChangedFilesVsDev lists what differs from dev (HEAD) within this PC's folder.
// Returns changed/added files (with bytes, keyed by repo-root-relative path) and
// the paths that were deleted. Uses -z so paths with spaces are safe.
func ChangedFilesVsDev(cfg AgentConfig, folder string) (map[string][]byte, []string, error) {
	out, err := gitCmd(cfg, "status", "--porcelain=v1", "-z", "--no-renames", "--", folder)
	if err != nil {
		return nil, nil, fmt.Errorf("git status: %v: %s", err, out)
	}
	changed := map[string][]byte{}
	var deleted []string
	for _, rec := range strings.Split(out, "\x00") {
		if len(rec) < 4 {
			continue
		}
		status, path := rec[:2], rec[3:]
		if path == "" {
			continue
		}
		if strings.Contains(status, "D") {
			deleted = append(deleted, filepath.ToSlash(path))
			continue
		}
		full := filepath.Join(cfg.RepoPath, filepath.FromSlash(path))
		data, rerr := os.ReadFile(full)
		if rerr != nil {
			log.Printf("[capture] WARN unreadable %s: %v", full, rerr)
			continue
		}
		changed[filepath.ToSlash(path)] = data
	}
	return changed, deleted, nil
}
