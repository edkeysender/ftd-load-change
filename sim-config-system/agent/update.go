package main

// Remote self-update. On an `update` command the agent downloads the current
// build from the coordinator, swaps its own exe, and relaunches. Works while
// running because Windows allows renaming (not overwriting) a running exe:
// rename self -> .old, move the new build into place, exec it, exit.

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
)

// Version is stamped at build time via -ldflags "-X main.Version=...".
var Version = "dev"

func cleanupOldBinary() {
	if self, err := os.Executable(); err == nil {
		_ = os.Remove(self + ".old") // leftover from a previous self-update
	}
}

func (a *Agent) doUpdate(c Command) {
	log.Printf("[update] downloading new agent from coordinator...")
	self, err := os.Executable()
	if err != nil {
		a.fail(err)
		return
	}
	self, _ = filepath.Abs(self)
	newPath, oldPath := self+".new", self+".old"

	if err := a.api.DownloadBinary(newPath); err != nil {
		a.fail(fmt.Errorf("download: %w", err))
		return
	}
	if fi, err := os.Stat(newPath); err != nil || fi.Size() < 1024 {
		_ = os.Remove(newPath)
		a.fail(fmt.Errorf("downloaded binary looks invalid"))
		return
	}

	_ = os.Remove(oldPath)
	if err := os.Rename(self, oldPath); err != nil { // move the running exe aside
		_ = os.Remove(newPath)
		a.fail(fmt.Errorf("rename self: %w", err))
		return
	}
	if err := os.Rename(newPath, self); err != nil { // put the new build in place
		_ = os.Rename(oldPath, self) // best-effort rollback
		a.fail(fmt.Errorf("install new: %w", err))
		return
	}

	log.Printf("[update] installed, relaunching...")
	cmd := exec.Command(self)
	cmd.Dir = filepath.Dir(self)
	cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, os.Stdin
	if err := cmd.Start(); err != nil {
		a.fail(fmt.Errorf("relaunch: %w", err))
		return
	}
	log.Printf("[update] new agent started; exiting old process")
	os.Exit(0)
}
