package main

// Guards: per-PC compliance items. Each guard has a PowerShell "check" script
// (exit 0 = compliant) and an "apply" script (exit 0 = fixed), optionally with
// asset files (wallpaper image, git bundle, …). The agent downloads the script
// (+ assets for apply), runs it, and reports the result. Read-only w.r.t. the
// versioned sim content — this provisions/keeps the machine's config.

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// runGuardChecks runs every guard's check once — called at agent launch so the
// dashboard shows fresh pass/fail without the browser having to trigger it.
func (a *Agent) runGuardChecks() {
	guards, err := a.api.GetGuards()
	if err != nil {
		log.Printf("[guard] check-on-launch: %v", err)
		return
	}
	for _, g := range guards {
		a.doGuard(Command{Type: "guard", GuardID: g.ID, GuardKind: "check",
			ScriptURL: "/guards/file/" + g.Check, ScriptName: g.Check})
	}
}

func (a *Agent) doGuard(c Command) {
	log.Printf("[guard] %s %s", c.GuardKind, c.GuardID)
	dir, err := os.MkdirTemp("", "sim-guard-"+sanitizeName(c.GuardID))
	if err != nil {
		a.api.GuardResult(a.cfg.PCIP, c.GuardID, c.GuardKind, false, "temp dir: "+err.Error())
		return
	}
	defer os.RemoveAll(dir)

	assetDir := filepath.Join(dir, "assets")
	if err := os.MkdirAll(assetDir, 0o755); err != nil {
		a.api.GuardResult(a.cfg.PCIP, c.GuardID, c.GuardKind, false, err.Error())
		return
	}
	for _, as := range c.GuardAssets {
		if err := a.api.DownloadFile(as.URL, filepath.Join(assetDir, as.Name)); err != nil {
			a.api.GuardResult(a.cfg.PCIP, c.GuardID, c.GuardKind, false, "download "+as.Name+": "+err.Error())
			return
		}
	}
	script := filepath.Join(dir, c.ScriptName) // keep the .ps1 extension
	if err := a.api.DownloadFile(c.ScriptURL, script); err != nil {
		a.api.GuardResult(a.cfg.PCIP, c.GuardID, c.GuardKind, false, "download script: "+err.Error())
		return
	}

	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script)
	// SIM_PC_IP is the IP the coordinator knows this PC by — the wol guard uses it to
	// pick the NIC to configure (a PC may have several). SIM_LHM is where the sensor
	// DLLs live, shared with the HealthCheck probe.
	cmd.Env = append(os.Environ(),
		"SIM_ASSETS="+assetDir,
		"SIM_PC_IP="+a.cfg.PCIP,
		"SIM_LHM="+a.lhmDir())
	out, runErr := cmd.CombinedOutput()
	ok := runErr == nil // exit code 0
	detail := strings.TrimSpace(string(out))
	if detail == "" && runErr != nil {
		detail = runErr.Error()
	}
	a.api.GuardResult(a.cfg.PCIP, c.GuardID, c.GuardKind, ok, detail)
	log.Printf("[guard] %s %s -> ok=%v", c.GuardKind, c.GuardID, ok)
}
