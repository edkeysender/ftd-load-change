package main

// Install prerequisites pushed from the coordinator (git bundle, VC++ redist, …).
// Two kinds: "run" downloads an installer exe and runs it silently with the given
// args; "unzip" downloads a zip and extracts it to a target dir (e.g. portable git).
// Read-only w.r.t. the sim's versioned content — this just provisions the machine.

import (
	"archive/zip"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func (a *Agent) doInstall(c Command) {
	log.Printf("[install] %s (%s)", c.Name, c.InstallType)
	name := c.File
	if name == "" {
		name = "sim-install-" + sanitizeName(c.InstallID)
	}
	tmp := filepath.Join(os.TempDir(), "sim-install-"+sanitizeName(c.InstallID)+"-"+name)
	if err := a.api.DownloadFile(c.URL, tmp); err != nil {
		a.installFail(c, fmt.Errorf("download: %w", err))
		return
	}
	defer os.Remove(tmp)

	var err error
	switch c.InstallType {
	case "unzip":
		if c.Target == "" {
			err = fmt.Errorf("no target for unzip install")
		} else {
			err = unzipTo(tmp, c.Target)
		}
	default: // "run"
		cmd := exec.Command(tmp, c.Args...)
		if out, e := cmd.CombinedOutput(); e != nil {
			err = fmt.Errorf("%v: %s", e, strings.TrimSpace(string(out)))
		}
	}
	if err != nil {
		a.installFail(c, err)
		return
	}
	a.api.InstallResult(a.cfg.PCIP, c.InstallID, true, c.Name+" installed")
	log.Printf("[install] %s done", c.Name)
}

func (a *Agent) installFail(c Command, err error) {
	log.Printf("[install] %s FAILED: %v", c.Name, err)
	a.api.InstallResult(a.cfg.PCIP, c.InstallID, false, err.Error())
}

func sanitizeName(s string) string {
	return strings.Map(func(r rune) rune {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
			return r
		}
		return '-'
	}, s)
}

// unzipTo extracts a zip into dir (created if needed), guarding against zip-slip.
func unzipTo(zipPath, dir string) error {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer r.Close()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	root := filepath.Clean(dir)
	for _, f := range r.File {
		dst := filepath.Join(dir, filepath.FromSlash(f.Name))
		if !strings.HasPrefix(filepath.Clean(dst), root+string(os.PathSeparator)) && filepath.Clean(dst) != root {
			return fmt.Errorf("unsafe path in zip: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
		if err != nil {
			rc.Close()
			return err
		}
		_, cErr := io.Copy(out, rc)
		out.Close()
		rc.Close()
		if cErr != nil {
			return cErr
		}
	}
	return nil
}
