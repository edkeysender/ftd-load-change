package main

// Read-only filesystem helpers for Phase 1 (import + size report).
// NOTHING here writes to live locations — bootstrap is strictly a mirror UP.

import (
	"crypto/sha1"
	"encoding/hex"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// liveLabels maps each of an app's `live` dirs to the subfolder it occupies
// inside the app's repo folder.
//
//   - 1 live dir  -> "" (its contents land directly under app.repo)
//   - N live dirs -> each gets a label so they don't collide. The label is the
//     dir's basename; if two live dirs share a basename (e.g. P3D's two
//     "Lockheed Martin" dirs) the colliding ones get a short path-hash suffix.
//
// Repo-relative path of a file = <label>/<path within live dir>, which is also
// what excludes are matched against (so per-app globs like "ProSim-AR/Navdata/**"
// work as written).
func liveLabels(live []string) map[string]string {
	labels := make(map[string]string, len(live))
	if len(live) == 1 {
		labels[live[0]] = ""
		return labels
	}
	seen := map[string]int{}
	for _, d := range live {
		seen[strings.ToLower(filepath.Base(d))]++
	}
	for _, d := range live {
		base := filepath.Base(d)
		if seen[strings.ToLower(base)] > 1 {
			h := sha1.Sum([]byte(filepath.ToSlash(d)))
			base = base + "__" + hex.EncodeToString(h[:])[:6]
		}
		labels[d] = base
	}
	return labels
}

// repoRel returns the forward-slash repo-relative path for a file under a live
// dir, given that live dir's label.
func repoRel(label, liveDir, fullPath string) string {
	rel, err := filepath.Rel(liveDir, fullPath)
	if err != nil {
		rel = filepath.Base(fullPath)
	}
	rel = filepath.ToSlash(rel)
	if label == "" {
		return rel
	}
	return label + "/" + rel
}

// excluded reports whether a repo-relative path matches any denylist glob.
// Supports ** (any depth), * (within a segment), and ? (single char).
func excluded(relPath string, patterns []string) bool {
	for _, p := range patterns {
		if globMatch(p, relPath) {
			return true
		}
	}
	return false
}

var globCache = map[string]*regexp.Regexp{}

func globMatch(pattern, name string) bool {
	re, ok := globCache[pattern]
	if !ok {
		re = regexp.MustCompile(globToRegex(pattern))
		globCache[pattern] = re
	}
	return re.MatchString(name)
}

// globToRegex converts a /-separated glob (with **, *, ?) to an anchored regex.
func globToRegex(glob string) string {
	glob = filepath.ToSlash(glob)
	var b strings.Builder
	b.WriteString("(?i)^") // case-insensitive: Windows paths
	for i := 0; i < len(glob); i++ {
		c := glob[i]
		switch c {
		case '*':
			if i+1 < len(glob) && glob[i+1] == '*' {
				// ** matches across path separators
				i++
				// consume an optional trailing slash so "a/**/b" and "a/**" both work
				if i+1 < len(glob) && glob[i+1] == '/' {
					i++
					b.WriteString("(?:.*/)?")
				} else {
					b.WriteString(".*")
				}
			} else {
				b.WriteString("[^/]*")
			}
		case '?':
			b.WriteString("[^/]")
		case '.', '+', '(', ')', '|', '^', '$', '{', '}', '[', ']', '\\':
			b.WriteByte('\\')
			b.WriteByte(c)
		default:
			b.WriteByte(c)
		}
	}
	b.WriteString("$")
	return b.String()
}

// walkApp walks every live dir of an app, applying excludes, and calls fn for
// each included regular file with its repo-relative path. Missing live dirs are
// skipped (logged by the caller via the returned list of missing dirs).
func walkApp(live []string, excludes []string, fn func(repoRelPath, fullPath string, size int64) error) (missing []string, err error) {
	labels := liveLabels(live)
	for _, dir := range live {
		info, statErr := os.Stat(dir)
		if statErr != nil || !info.IsDir() {
			missing = append(missing, dir)
			continue
		}
		label := labels[dir]
		walkErr := filepath.Walk(dir, func(path string, fi os.FileInfo, e error) error {
			if e != nil {
				return nil // skip unreadable entries rather than aborting the import
			}
			if fi.IsDir() {
				return nil
			}
			rel := repoRel(label, dir, path)
			if excluded(rel, excludes) {
				return nil
			}
			return fn(rel, path, fi.Size())
		})
		if walkErr != nil {
			return missing, walkErr
		}
	}
	return missing, nil
}

// dirSize returns the total bytes of an app's included files across its live dirs.
func appSize(live []string, excludes []string) (int64, []string) {
	var total int64
	missing, _ := walkApp(live, excludes, func(_, _ string, size int64) error {
		total += size
		return nil
	})
	return total, missing
}
