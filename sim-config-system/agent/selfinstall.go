package main

// Self-install: `simagent.exe -install` registers a scheduled task that starts the agent
// ELEVATED at every logon, so admin-only guards (clock, ssh, Windows Update, CPU temps)
// work without a UAC prompt each launch. The first -install needs admin once - if we are
// not elevated we relaunch ourselves through UAC. The task runs AS the logged-in user
// (not SYSTEM) on purpose: the notifications / recycle-bin / wallpaper guards write the
// console user's own registry hive, which a SYSTEM process would miss.

import (
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const (
	taskName   = "sim-agent"
	installDir = `C:\sim-agent`
)

// isElevated reports whether we hold a full administrator token. Opening a raw physical
// drive succeeds only for administrators - a pure-stdlib probe (no x/sys import needed).
func isElevated() bool {
	f, err := os.Open(`\\.\PHYSICALDRIVE0`)
	if err != nil {
		return false
	}
	_ = f.Close()
	return true
}

func installTask() {
	exe, err := os.Executable()
	if err != nil {
		log.Fatalf("[install] cannot find own path: %v", err)
	}

	if !isElevated() {
		// Relaunch elevated via UAC (one prompt). PowerShell is always present on Windows.
		log.Printf("[install] requesting administrator rights - accept the UAC prompt...")
		ps := fmt.Sprintf("Start-Process -FilePath '%s' -ArgumentList '-install' -Verb RunAs",
			strings.ReplaceAll(exe, "'", "''"))
		if err := exec.Command("powershell", "-NoProfile", "-Command", ps).Run(); err != nil {
			log.Fatalf("[install] elevation failed: %v (run it from an elevated PowerShell instead)", err)
		}
		log.Printf("[install] elevated installer launched in a new window.")
		return
	}

	// Elevated. Keep a stable copy in installDir (also where state + the sensor DLLs live).
	if err := os.MkdirAll(installDir, 0o755); err != nil {
		log.Fatalf("[install] cannot create %s: %v", installDir, err)
	}
	target := filepath.Join(installDir, "simagent.exe")
	if !samePath(exe, target) {
		if err := copyFile(exe, target); err != nil {
			if _, statErr := os.Stat(target); statErr != nil {
				log.Fatalf("[install] cannot copy the agent to %s: %v", target, err)
			}
			log.Printf("[install] could not overwrite %s (%v) - keeping the existing copy", target, err)
		} else {
			log.Printf("[install] installed to %s", target)
		}
	}

	// Run as the current interactive user (elevation kept the same user). /IT = interactive
	// token (no stored password), /RL HIGHEST = elevated, /SC ONLOGON = at each logon.
	user := os.Getenv("USERDOMAIN") + `\` + os.Getenv("USERNAME")
	args := []string{"/Create", "/TN", taskName, "/TR", `"` + target + `"`,
		"/SC", "ONLOGON", "/RL", "HIGHEST", "/RU", user, "/IT", "/F"}
	if out, err := exec.Command("schtasks.exe", args...).CombinedOutput(); err != nil {
		log.Fatalf("[install] schtasks create failed: %v\n%s", err, strings.TrimSpace(string(out)))
	}
	log.Printf("[install] scheduled task %q registered - elevated, at logon, as %s", taskName, user)

	// Start it now so the agent is up without waiting for a re-logon. The task instance
	// runs in the background; this -install process exits so there is only one agent.
	_ = exec.Command("schtasks.exe", "/Run", "/TN", taskName).Run()
	log.Printf("[install] done - the agent now starts elevated automatically at every sign-in")
}

func samePath(a, b string) bool {
	pa, ea := filepath.Abs(a)
	pb, eb := filepath.Abs(b)
	return ea == nil && eb == nil && strings.EqualFold(pa, pb)
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, in)
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	return err
}
