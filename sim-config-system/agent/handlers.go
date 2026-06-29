package main

import (
	"encoding/base64"
	"fmt"
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
	const budget = 48 << 20 // upload in ~48MB batches so huge folders don't OOM

	batch := map[string][]byte{}
	var batchBytes, totalBytes int64
	var totalFiles, batchIdx int
	var missing []string
	failed := false

	upload := func(final bool) bool {
		if err := a.api.UploadImportBundle(a.cfg.PCIP, c.Folder, batch, missing, batchIdx, final); err != nil {
			a.fail(err)
			failed = true
			return false
		}
		batch = map[string][]byte{} // free the batch's memory
		batchBytes = 0
		batchIdx++
		return true
	}

	for name, app := range c.Apps {
		if app.Repo == "" || len(app.Live) == 0 {
			continue
		}
		miss, err := walkApp(app.Live, app.Exclude, func(rel, full string, size int64) error {
			data, rerr := os.ReadFile(full)
			if rerr != nil {
				log.Printf("[import] WARN unreadable %s: %v", full, rerr)
				return nil // skip, don't abort the whole import
			}
			batch[app.Repo+"/"+rel] = data
			batchBytes += int64(len(data))
			totalBytes += size
			totalFiles++
			if batchBytes >= budget && !upload(false) {
				return fmt.Errorf("batch upload failed")
			}
			return nil
		})
		if failed {
			return
		}
		if err != nil {
			a.fail(err)
			return
		}
		for _, m := range miss {
			missing = append(missing, name+": "+m)
			log.Printf("[import] WARN missing live dir for %s: %s", name, m)
		}
	}

	log.Printf("[import] %d files, %d bytes in %d batch(es); finalizing", totalFiles, totalBytes, batchIdx+1)
	if upload(true) {
		log.Printf("[import] done folder=%s", c.Folder)
	}
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

// doDeploy (Phase 2): fetch -> sparse checkout the ref -> mirror worktree->live
// (honoring excludes) -> restart apps in start_delay order. Local edits to
// versioned files are overwritten — the determinism guarantee.
func (a *Agent) doDeploy(c Command) {
	a.setState(StateDeploying)
	log.Printf("[deploy] ref=%s folder=%s apps=%d", c.Ref, c.Folder, len(c.Apps))
	if c.GitRemote != "" {
		a.cfg.GitRemote = c.GitRemote
	}
	if err := GitFetchCheckout(a.cfg, c.Folder, c.Ref); err != nil {
		a.fail(err)
		return
	}
	StopApps(c.Apps)                                  // stop first: nothing runs during sync
	if err := MirrorToLive(a.cfg, c.Apps); err != nil { // worktree -> live, honoring excludes
		a.fail(err)
		return
	}
	StartApps(c.Apps)   // launch in start_delay order — only after the full sync
	a.remember(c, true) // store apps+ref; just-deployed => clean
	a.setState(StateTraining)
	a.api.DeployResult(a.cfg.PCIP, c.Folder, "TRAINING", c.Ref, true)
	log.Printf("[deploy] done -> TRAINING @ %s", c.Ref)
}

// doTrack (Phase 3 driver): same mirror as deploy but tracks the dev branch and
// stays in DEV_TRACKING so testers see changes live.
func (a *Agent) doTrack(c Command) {
	a.setState(StateDeploying)
	log.Printf("[track] ref=%s folder=%s apps=%d", c.Ref, c.Folder, len(c.Apps))
	if c.GitRemote != "" {
		a.cfg.GitRemote = c.GitRemote
	}
	if err := GitFetchCheckout(a.cfg, c.Folder, c.Ref); err != nil {
		a.fail(err)
		return
	}
	StopApps(c.Apps)
	if err := MirrorToLive(a.cfg, c.Apps); err != nil {
		a.fail(err)
		return
	}
	StartApps(c.Apps)
	a.remember(c, true)
	a.setState(StateDevTracking)
	log.Printf("[track] done -> DEV_TRACKING @ %s", c.Ref)
}

// doCapture (Phase 3): checkout dev (clean base), quiesce apps, mirror live->
// worktree, compute the diff vs dev, upload the bundle, then relaunch apps and
// return to DEV_TRACKING. The coordinator commits the bundle to dev under the lock.
func (a *Agent) doCapture(c Command) {
	a.setState(StateCapturing)
	log.Printf("[capture] folder=%s apps=%d", c.Folder, len(c.Apps))
	if c.GitRemote != "" {
		a.cfg.GitRemote = c.GitRemote
	}
	if err := GitFetchCheckout(a.cfg, c.Folder, c.Ref); err != nil { // ref = dev
		a.fail(err)
		return
	}
	StopApps(c.Apps) // close file handles before snapshotting
	if err := MirrorToWorktree(a.cfg, c.Apps); err != nil {
		a.fail(err)
		return
	}
	changed, deleted, err := ChangedFilesVsDev(a.cfg, c.Folder)
	if err != nil {
		a.fail(err)
		return
	}
	log.Printf("[capture] %d changed, %d deleted; uploading", len(changed), len(deleted))
	if err := a.api.UploadCaptureBundle(a.cfg.PCIP, c.Folder, changed, deleted); err != nil {
		a.fail(err)
		return
	}
	StartApps(c.Apps) // resume the dev session (apps were stopped above)
	a.remember(c, true)
	a.setState(StateDevTracking)
	log.Printf("[capture] done")
}

// doBrowse (config panel): list a directory level (or drives) and post the result
// back, correlated by ReqID. Read-only; does not change agent state.
func (a *Agent) doBrowse(c Command) {
	var entries []BrowseEntry
	var errStr string
	if c.Path == "" {
		entries = listDrives()
	} else if e, err := listDir(c.Path); err != nil {
		errStr = err.Error()
	} else {
		entries = e
	}
	a.api.BrowseResult(a.cfg.PCIP, c.ReqID, c.Path, entries, errStr)
}

func (a *Agent) fail(err error) {
	log.Printf("ERROR: %v", err)
	a.setState(StateError)
}

func b64(b []byte) string { return base64.StdEncoding.EncodeToString(b) }
