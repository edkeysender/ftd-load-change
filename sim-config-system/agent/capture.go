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
	"path/filepath"
	"strings"
)

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

// ChangedFilesVsDev lists what differs from dev (HEAD) within this PC's folder:
// repo-root-relative paths of changed/added files (bytes read later, in batches,
// to bound memory) and the paths that were deleted. -z so spaces are safe.
func ChangedFilesVsDev(cfg AgentConfig, folder string) (changed []string, deleted []string, err error) {
	out, e := gitCmd(cfg, "status", "--porcelain=v1", "-z", "--no-renames", "--", folder)
	if e != nil {
		return nil, nil, fmt.Errorf("git status: %v: %s", e, out)
	}
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
		} else {
			changed = append(changed, filepath.ToSlash(path))
		}
	}
	return changed, deleted, nil
}
