package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sync"
	"time"
)

type Client struct {
	mu    sync.RWMutex // guards base: it changes if the coordinator moves mid-run
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
	Error      string `json:"error,omitempty"`
	MAC        string `json:"mac,omitempty"`
	Host       string `json:"host,omitempty"`
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

// localIPv4s lists this machine's non-loopback IPv4 addresses. On a dual-homed PC
// there are several; the coordinator uses them to pin our identity to the manifest
// IP instead of whichever interface routing happened to use for this request.
func localIPv4s() []string {
	var ips []string
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return ips
	}
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok && !n.IP.IsLoopback() {
			if v4 := n.IP.To4(); v4 != nil {
				ips = append(ips, v4.String())
			}
		}
	}
	return ips
}

// (/whoami lives in coordinator.go as probeWhoami: identity is resolved against a
// candidate address on a short-timeout client, before any long-lived client exists.)

func NewClient(cfg AgentConfig) *Client {
	return &Client{
		base:  cfg.CoordinatorURL,
		token: cfg.Token,
		pcIP:  cfg.PCIP,
		// Imports can move large trees; allow a generous timeout.
		http: &http.Client{Timeout: 10 * time.Minute},
	}
}

// SetBase repoints the client at a different coordinator. Called when the Pi's
// address changes under a running agent (see recoverCoordinator): swapping the URL
// in place keeps every existing *Client holder — heartbeat, health loop, poll loop —
// pointing at the same object instead of racing on a replaced pointer.
func (c *Client) SetBase(url string) {
	c.mu.Lock()
	c.base = url
	c.mu.Unlock()
}

func (c *Client) Base() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.base
}

func (c *Client) do(method, path string, body any) (*http.Response, error) {
	var r io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		r = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, c.Base()+path, r)
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

// PostHealth uploads one HealthCheck sample. Best-effort like the heartbeat: a lost
// sample is a gap in a 30-day chart, not something worth failing the agent over.
func (c *Client) PostHealth(ip string, s *HealthSample) {
	resp, err := c.do("POST", "/agents/"+ip+"/health", s)
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

// UpdatePending reports whether the operator asked this agent to self-update.
func (c *Client) UpdatePending() (bool, error) {
	resp, err := c.do("GET", "/agents/"+c.pcIP+"/update-pending", nil)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	var out struct {
		Pending bool `json:"pending"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return false, err
	}
	return out.Pending, nil
}

// AckUpdate clears the pending-update flag (called once the new binary is in
// place) so the agent doesn't update-loop on the next startup.
func (c *Client) AckUpdate(pcIP string) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/ack-update", map[string]any{})
	if err == nil {
		resp.Body.Close()
	}
}

// DownloadBinary fetches the current agent build from the coordinator into dest.
func (c *Client) DownloadBinary(dest string) error {
	return c.DownloadFile("/agent/binary", dest)
}

// DownloadFile streams a coordinator path into a local file.
func (c *Client) DownloadFile(path, dest string) error {
	resp, err := c.do("GET", path, nil)
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

// InstallResult reports the outcome of an install command to the coordinator.
func (c *Client) InstallResult(pcIP, id string, ok bool, msg string) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/install-result", map[string]any{
		"id": id, "ok": ok, "msg": msg,
	})
	if err == nil {
		resp.Body.Close()
	}
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

// DriftResult posts the list of files that differ between the deployed version and
// live, answering an operator's "diff" request (correlated by reqID).
func (c *Client) DriftResult(pcIP, reqID string, entries []DriftEntry) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/drift-result", map[string]any{
		"req_id": reqID, "entries": entries,
	})
	if err == nil {
		resp.Body.Close()
	}
}

// GuardDef is the minimal guard info the agent needs to run its checks on launch.
type GuardDef struct {
	ID    string `json:"id"`
	Check string `json:"check"`
}

// GetGuards fetches the guard definitions so the agent can self-check at startup.
func (c *Client) GetGuards() ([]GuardDef, error) {
	resp, err := c.do("GET", "/guards", nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("guards: %s", resp.Status)
	}
	var out struct {
		Guards []GuardDef `json:"guards"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out.Guards, nil
}

// GuardResult reports the outcome of a guard check/apply to the coordinator.
func (c *Client) GuardResult(pcIP, id, kind string, ok bool, detail string) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/guard-result", map[string]any{
		"id": id, "kind": kind, "ok": ok, "detail": detail,
	})
	if err == nil {
		resp.Body.Close()
	}
}

// FileDiffResult posts one file's version-vs-live contents for the UI diff.
func (c *Client) FileDiffResult(pcIP, reqID string, d FileDiff) {
	resp, err := c.do("POST", "/agents/"+pcIP+"/filediff-result", map[string]any{
		"req_id": reqID, "diff": d,
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
