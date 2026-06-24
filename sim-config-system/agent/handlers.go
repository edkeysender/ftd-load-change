package main

import (
	"encoding/base64"
	"log"
	"os"
)

// ============================ command handlers ==========================

// doImport (Phase 1): bootstrap. Read-only on this PC — mirror the app trees
// UP to the coordinator. NEVER writes to live locations, NEVER restarts apps.
//
// For each app the coordinator sent, walk its `live` dirs honoring the merged
// excludes, read each included file, and key it by its repo-relative path under
// the app's repo folder (e.g. "pc-12-display/displays/CPTInboard/foo.cfg").
// The whole PC folder is uploaded as one bundle; the coordinator stages it into
// its working clone (idempotent: re-import replaces the folder's tree).
func (a *Agent) doImport(c Command) {
	log.Printf("[import] folder=%s apps=%d (read-only)", c.Folder, len(c.Apps))
	files := map[string][]byte{}
	var missing []string
	var totalBytes int64

	for name, app := range c.Apps {
		if app.Repo == "" || len(app.Live) == 0 {
			log.Printf("[import] skip %s (no repo/live to version)", name)
			continue
		}
		miss, err := walkApp(app.Live, app.Exclude, func(rel, full string, size int64) error {
			data, rerr := os.ReadFile(full)
			if rerr != nil {
				log.Printf("[import] WARN unreadable %s: %v", full, rerr)
				return nil // skip, don't abort the whole import
			}
			files[app.Repo+"/"+rel] = data
			totalBytes += size
			return nil
		})
		if err != nil {
			a.fail(err)
			return
		}
		for _, m := range miss {
			missing = append(missing, name+": "+m)
			log.Printf("[import] WARN missing live dir for %s: %s", name, m)
		}
	}

	log.Printf("[import] bundled %d files, %d bytes; uploading", len(files), totalBytes)
	if err := a.api.UploadImportBundle(a.cfg.PCIP, c.Folder, missing, files); err != nil {
		a.fail(err)
		return
	}
	log.Printf("[import] done folder=%s", c.Folder)
}

// doSizeReport (Phase 1): du -sh per app folder so the operator sees GB before
// sealing and can drop surprises into exclude. Read-only.
func (a *Agent) doSizeReport(c Command) {
	log.Printf("[size_report] apps=%d", len(c.Apps))
	sizes := map[string]int64{}
	for name, app := range c.Apps {
		if len(app.Live) == 0 {
			sizes[name] = 0
			continue
		}
		bytes, miss := appSize(app.Live, app.Exclude)
		sizes[name] = bytes
		for _, m := range miss {
			log.Printf("[size_report] WARN missing live dir for %s: %s", name, m)
		}
		log.Printf("[size_report] %s = %d bytes", name, bytes)
	}
	if err := a.api.PostSizeReport(a.cfg.PCIP, c.Folder, sizes); err != nil {
		a.fail(err)
	}
}

// doDeploy (Phase 2): fetch -> sparse checkout training-live -> mirror worktree->live -> restart.
func (a *Agent) doDeploy(c Command) {
	a.state = StateDeploying
	log.Printf("[deploy] ref=%s", c.Ref)
	if err := GitFetchCheckout(a.cfg, c.Ref); err != nil {
		a.fail(err)
		return
	}
	if err := MirrorToLive(a.cfg); err != nil { // worktree -> live, honoring excludes
		a.fail(err)
		return
	}
	RestartApps(a.cfg) // launch in start_delay order
	a.state = StateTraining
}

// doTrack (Phase 3): switch to DEV_TRACKING on the dev branch.
func (a *Agent) doTrack(c Command) {
	log.Printf("[track] ref=%s", c.Ref)
	if err := GitFetchCheckout(a.cfg, c.Ref); err != nil {
		a.fail(err)
		return
	}
	MirrorToLive(a.cfg)
	RestartApps(a.cfg)
	a.state = StateDevTracking
}

// doCapture (Phase 3): quiesce apps, mirror live->worktree, bundle changed files, upload.
func (a *Agent) doCapture(c Command) {
	a.state = StateCapturing
	log.Printf("[capture] folder=%s", c.Folder)
	QuiesceApps(a.cfg) // graceful stop so files aren't mid-write (OPEN Q: force-kill fallback)
	if err := MirrorToWorktree(a.cfg); err != nil { // live -> worktree, honoring excludes
		a.fail(err)
		return
	}
	files, err := ChangedFilesVsDev(a.cfg) // map[relpath][]byte for changes vs dev
	if err != nil {
		a.fail(err)
		return
	}
	if err := a.api.UploadCaptureBundle(a.cfg.PCIP, c.Folder,
		"dev capture", "TODO-session-user", files); err != nil {
		a.fail(err)
		return
	}
	a.state = StateDevTracking
}

func (a *Agent) fail(err error) {
	log.Printf("ERROR: %v", err)
	a.state = StateError
}

func b64(b []byte) string { return base64.StdEncoding.EncodeToString(b) }

// ============================ platform stubs ============================
// These are the Windows-specific pieces to implement. Kept as stubs so the
// poll loop and state machine compile and can be exercised end-to-end first.

func GitFetchCheckout(cfg AgentConfig, ref string) error {
	// TODO: cfg.GitExe fetch; sparse-checkout set cfg.Folder; checkout ref.
	return nil
}

func MirrorToLive(cfg AgentConfig) error {
	// TODO: robocopy-equivalent worktree(cfg.RepoPath/folder/app.repo) -> app.live,
	// /MIR with exclude globs. P3D = config dirs only.
	return nil
}

func MirrorToWorktree(cfg AgentConfig) error {
	// TODO: reverse mirror app.live -> worktree, honoring excludes.
	return nil
}

func ChangedFilesVsDev(cfg AgentConfig) (map[string][]byte, error) {
	// TODO: git status --porcelain in the sparse folder; read changed file bytes.
	return map[string][]byte{}, nil
}

func RestartApps(cfg AgentConfig) {
	// TODO: launch each app via app.run after app.start_delay seconds, in order.
	//       Honor restart_on_change with a file watcher in DEV_TRACKING.
}

func QuiesceApps(cfg AgentConfig) {
	// TODO: graceful stop of apps whose files are about to be captured.
}

func CheckClean(cfg AgentConfig) bool {
	// TODO: true if worktree matches training-live (no drift).
	return true
}
