package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"gopkg.in/yaml.v3"
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
}

func LoadConfig(path string) AgentConfig {
	b, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	var c AgentConfig
	if err := yaml.Unmarshal(b, &c); err != nil {
		panic(err)
	}
	return c
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
	resp, err := c.do("GET", "/agents/"+c.pcIP+"/commands", nil)
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

func (c *Client) UploadCaptureBundle(pcIP, folder, message, author string, files map[string][]byte) error {
	enc := map[string]string{}
	for p, b := range files {
		enc[p] = b64(b)
	}
	resp, err := c.do("POST", "/agents/"+pcIP+"/capture-result", map[string]any{
		"folder": folder, "message": message, "author": author, "files": enc,
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

// UploadImportBundle posts a PC's imported tree (Phase 1 bootstrap). Files are
// keyed by repo-relative path under the PC folder. The coordinator stages them
// into its working clone; nothing is committed until /seal-baseline.
func (c *Client) UploadImportBundle(pcIP, folder string, missing []string, files map[string][]byte) error {
	enc := make(map[string]string, len(files))
	for p, b := range files {
		enc[p] = b64(b)
	}
	resp, err := c.do("POST", "/agents/"+pcIP+"/import-result", map[string]any{
		"folder": folder, "missing": missing, "files": enc,
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
