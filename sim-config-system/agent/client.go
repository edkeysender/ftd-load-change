package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"time"
)

type Client struct {
	base  string
	token string
	pcIP  string
	http  *http.Client
}

type Heartbeat struct {
	PCIP       string `json:"pc_ip"`
	Folder     string `json:"folder"`
	Mode       string `json:"mode"`
	CurrentRef string `json:"current_ref,omitempty"`
	Clean      bool   `json:"clean"`
	Version    string `json:"version,omitempty"`
}

// LoadConfig reads an OPTIONAL agent.json. Missing file is fine (zero-config
// agent); a malformed file is warned about but not fatal.
func LoadConfig(path string) AgentConfig {
	var c AgentConfig
	b, err := os.ReadFile(path)
	if err != nil {
		return c // no agent.json -> rely on baked defaults + /whoami
	}
	if err := json.Unmarshal(b, &c); err != nil {
		log.Printf("warning: ignoring malformed %s: %v", path, err)
	}
	return c
}

// Whoami asks the coordinator which IP it sees us as (authoritative identity) and
// the manifest folder for that IP (may be empty if the PC isn't in the manifest).
func (c *Client) Whoami() (ip, folder string, err error) {
	resp, err := c.do("GET", "/whoami", nil)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", "", fmt.Errorf("whoami: %s", resp.Status)
	}
	var out struct {
		IP     string `json:"ip"`
		Folder string `json:"folder"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", "", err
	}
	return out.IP, out.Folder, nil
}

func NewClient(cfg AgentConfig) *Client {
	return &Client{
		base:  cfg.CoordinatorURL,
		token: cfg.Token,
		pcIP:  cfg.PCIP,
		// Imports can move large trees; allow a generous timeout.
		http: &http.Client{Timeout: 10 * time.Minute},
	}
}

func (c *Client) do(method, path string, body any) (*http.Response, error) {
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, c.base+path, r)
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	return c.http.Do(req)
}

func (c *Client) Heartbeat(hb Heartbeat) {
	resp, err := c.do("POST", "/agents/"+hb.PCIP+"/heartbeat", hb)
	if err == nil {
		resp.Body.Close()
	}
}

// PollCommand long-polls for the next command (nil if none pending).
func (c *Client) PollCommand() (*Command, error) {
	return c.fetchCommand("/agents/" + c.pcIP + "/commands")
}

// GetEnforce asks the coordinator for the current training-live deploy command
// (used once at startup to sync + launch before anything runs). nil if nothing
// to enforce (no training-live yet).
func (c *Client) GetEnforce() (*Command, error) {
	return c.fetchCommand("/agents/" + c.pcIP + "/enforce")
}

func (c *Client) fetchCommand(path string) (*Command, error) {
	resp, err := c.do("GET", path, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out struct {
		Command *Command `json:"command"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out.Command, nil
}

// UploadCaptureBundle posts the dev-capture diff (changed files + deletions). The
// agent does not set the author/message — the coordinator attributes the commit
// to the dev-session lock holder.
func (c *Client) UploadCaptureBundle(pcIP, folder string, changed map[string][]byte, deleted []string, batchIndex int, final bool, totalBytes int64) error {
	enc := make(map[string]string, len(changed))
	for p, b := range changed {
		enc[p] = b64(b)
	}
	if deleted == nil {
		deleted = []string{} // send [] not null so the coordinator never len(None)s
	}
	resp, err := c.do("POST", "/agents/"+pcIP+"/capture-result", map[string]any{
		"folder": folder, "files": enc, "deleted": deleted,
		"batch_index": batchIndex, "final": final, "total_bytes": totalBytes,
	})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("capture upload failed: %s", resp.Status)
	}
	return nil
}

// UploadImportBundle posts ONE batch of a PC's imported tree (Phase 1 bootstrap).
// Large folders are streamed in batches to bound memory. batchIndex 0 tells the
// coordinator to clear the folder first; final=true records the import.
func (c *Client) UploadImportBundle(pcIP, folder string, files map[string][]byte, missing []string, batchIndex int, final bool, totalBytes int64) error {
	enc := make(map[string]string, len(files))
	for p, b := range files {
		enc[p] = b64(b)
	}
	resp, err := c.do("POST", "/agents/"+pcIP+"/import-result", map[string]any{
		"folder": folder, "missing": missing, "files": enc,
		"batch_index": batchIndex, "final": final, "total_bytes": totalBytes,
	})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("import upload failed: %s", resp.Status)
	}
	return nil
}

// DeployResult reports the outcome of a deploy so the UI updates immediately
// (the periodic heartbeat would otherwise carry it a few seconds later).
func (c *Client) DeployResult(pcIP, folder, mode, ref string, clean bool) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/deploy-result", map[string]any{
		"folder": folder, "mode": mode, "ref": ref, "clean": clean,
	})
	if err == nil {
		resp.Body.Close()
	}
}

// DownloadBinary fetches the current agent build from the coordinator into dest.
func (c *Client) DownloadBinary(dest string) error {
	resp, err := c.do("GET", "/agent/binary", nil)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("download failed: %s", resp.Status)
	}
	f, err := os.Create(dest)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, resp.Body)
	return err
}

// BrowseResult returns a directory listing for a config-panel browse request.
func (c *Client) BrowseResult(pcIP, reqID, path string, entries []BrowseEntry, errStr string) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/browse-result", map[string]any{
		"req_id": reqID, "path": path, "entries": entries, "error": errStr,
	})
	if err == nil {
		resp.Body.Close()
	}
}

// PostSizeReport posts per-app byte sizes for the bootstrap panel.
func (c *Client) PostSizeReport(pcIP, folder string, sizes map[string]int64) error {
	resp, err := c.do("POST", "/agents/"+pcIP+"/size-report-result", map[string]any{
		"folder": folder, "sizes": sizes,
	})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("size report failed: %s", resp.Status)
	}
	return nil
}
