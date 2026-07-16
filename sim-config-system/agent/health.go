package main

// HealthCheck: periodically read this PC's BIOS + CPU/GPU temperatures and post
// them to the coordinator, which keeps 30 days of history. Read-only — it samples
// sensors and touches nothing else, so it runs regardless of the agent's state
// (even UNSEEDED: a PC with no load still has a temperature worth watching).
//
// The probe itself is a PowerShell script fetched from the coordinator rather than
// baked in here, so the sensor logic can be fixed on the Pi without rebuilding and
// redeploying every agent.

import (
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	healthScript   = "health-probe.ps1"
	healthInterval = 5 * time.Minute // matches the coordinator's HEALTH_SAMPLE_SECONDS
)

// HealthSample is health-probe.ps1's JSON. Temps are pointers: a nil CPUC means
// "this PC could not read one" (no thermal zone + no LibreHardwareMonitor), which
// is different from 0 °C and must survive the round trip as null.
type HealthSample struct {
	BiosVendor  string   `json:"bios_vendor,omitempty"`
	BiosVersion string   `json:"bios_version,omitempty"`
	BiosDate    string   `json:"bios_date,omitempty"`
	CPUName     string   `json:"cpu_name,omitempty"`
	GPUName     string   `json:"gpu_name,omitempty"`
	CPUC        *float64 `json:"cpu_c"`
	GPUC        *float64 `json:"gpu_c"`
	CPUSrc      string   `json:"cpu_src,omitempty"`
	GPUSrc      string   `json:"gpu_src,omitempty"`
	Note        string   `json:"note,omitempty"`
}

// lhmDir is where sensors-apply.ps1 puts the LibreHardwareMonitor DLLs — next to the
// agent, not in the repo (they're machine setup, not versioned sim content).
func (a *Agent) lhmDir() string { return filepath.Join(filepath.Dir(a.cfg.RepoPath), "lhm") }

func (a *Agent) runHealthLoop() {
	for {
		a.sampleAndPost()
		time.Sleep(healthInterval)
	}
}

// sampleAndPost reads the sensors once and uploads the result. Used by the 5-minute
// loop and by the on-demand "health" command (the dashboard's manual temp refresh).
func (a *Agent) sampleAndPost() {
	s, err := a.sampleHealth()
	if err != nil {
		log.Printf("[health] %v", err)
		return
	}
	a.api.PostHealth(a.cfg.PCIP, s)
}

func (a *Agent) sampleHealth() (*HealthSample, error) {
	dir, err := os.MkdirTemp("", "sim-health")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)

	script := filepath.Join(dir, healthScript)
	if err := a.api.DownloadFile("/guards/file/"+healthScript, script); err != nil {
		return nil, err
	}
	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script)
	cmd.Env = append(os.Environ(), "SIM_LHM="+a.lhmDir())
	out, err := cmd.Output() // stdout only: the probe prints one JSON object
	if err != nil {
		return nil, err
	}
	var s HealthSample
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(out))), &s); err != nil {
		return nil, err
	}
	return &s, nil
}
