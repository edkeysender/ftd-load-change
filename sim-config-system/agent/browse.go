package main

// Read-only filesystem browsing for the config panel's file tree. The agent
// lists drives / a directory level on demand; the coordinator proxies it to the
// UI. Never writes anything.

import (
	"os"
	"path/filepath"
	"sort"
)

// BrowseEntry is one item in a directory listing.
type BrowseEntry struct {
	Name  string `json:"name"`
	Path  string `json:"path"`   // forward-slash absolute path
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`   // file size in bytes (0 for dirs — not walked)
}

// listDrives returns the existing drive roots (C:/, D:/ ...).
func listDrives() []BrowseEntry {
	var out []BrowseEntry
	for c := 'A'; c <= 'Z'; c++ {
		root := string(c) + ":/"
		if fi, err := os.Stat(root); err == nil && fi.IsDir() {
			out = append(out, BrowseEntry{Name: string(c) + ":", Path: root, IsDir: true})
		}
	}
	return out
}

// listDir returns one level of a directory: dirs first, then files, alphabetical.
func listDir(path string) ([]BrowseEntry, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	var out []BrowseEntry
	for _, e := range entries {
		full := filepath.ToSlash(filepath.Join(path, e.Name()))
		var size int64
		if !e.IsDir() {
			if info, ierr := e.Info(); ierr == nil {
				size = info.Size()
			}
		}
		out = append(out, BrowseEntry{Name: e.Name(), Path: full, IsDir: e.IsDir(), Size: size})
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].IsDir != out[j].IsDir {
			return out[i].IsDir // dirs first
		}
		return out[i].Name < out[j].Name
	})
	return out, nil
}
