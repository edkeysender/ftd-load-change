package main

// The agent persists its last-adopted load (ref + app specs) next to the repo, so
// after a restart it can show the correct mode + drift even when the coordinator is
// unreachable or hasn't been told which load is live. This is NEVER used to sync —
// only to know what to compare live files against. A Deploy always overwrites it.

import (
	"encoding/json"
	"os"
	"path/filepath"
)

type savedLoad struct {
	Ref  string             `json:"ref"`
	Apps map[string]AppSpec `json:"apps"`
}

func (a *Agent) stateFile() string {
	return filepath.Join(filepath.Dir(a.cfg.RepoPath), "sim-agent-load.json")
}

// saveLoad records the currently-adopted load. Best-effort; failures are ignored.
func (a *Agent) saveLoad(ref string, apps map[string]AppSpec) {
	if ref == "" {
		return
	}
	if b, err := json.Marshal(savedLoad{Ref: ref, Apps: apps}); err == nil {
		_ = os.WriteFile(a.stateFile(), b, 0o644)
	}
}

// loadSavedLoad returns the last-adopted load as a deploy-shaped Command, or nil if
// none was persisted. Used as a fallback when the coordinator can't tell us the load.
func (a *Agent) loadSavedLoad() *Command {
	b, err := os.ReadFile(a.stateFile())
	if err != nil {
		return nil
	}
	var s savedLoad
	if json.Unmarshal(b, &s) != nil || s.Ref == "" {
		return nil
	}
	return &Command{Type: "deploy", Ref: s.Ref, Apps: s.Apps}
}
