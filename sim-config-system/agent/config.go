package main

// AgentConfig is read from a local agent.json on each PC. JSON (not YAML) so the
// agent stays a zero-dependency, fully static binary.
type AgentConfig struct {
	PCIP           string `json:"pc_ip"`           // this PC's IP, key into the manifest
	Folder         string `json:"folder"`          // its monorepo folder, e.g. pc-12-display
	CoordinatorURL string `json:"coordinator_url"` // e.g. http://70.84.68.196:8090
	Token          string `json:"token"`           // shared bearer token (OPEN Q: mTLS)
	RepoPath       string `json:"repo_path"`       // local sparse clone path, e.g. D:/sim-config (deploy only)
	GitRemote      string `json:"git_remote"`      // Forgejo URL (deploy only; not needed for import)
	GitExe         string `json:"git_exe"`         // git.exe path (deploy only); "git" if on PATH

	// On startup, sync training-live then launch the apps (replaces Windows
	// auto-start so nothing runs before the sync completes). Default true; set
	// false for a passive agent (e.g. during testing).
	EnforceOnStart *bool `json:"enforce_on_start"`
}

func (c AgentConfig) enforceOnStart() bool {
	return c.EnforceOnStart == nil || *c.EnforceOnStart
}

// State is the agent's place in the lifecycle (see PROJECT_SPEC.md section 8).
type State string

const (
	StateUnseeded    State = "UNSEEDED"
	StateIdle        State = "IDLE"
	StateDeploying   State = "DEPLOYING"
	StateTraining    State = "TRAINING"
	StateDevTracking State = "DEV_TRACKING"
	StateCapturing   State = "CAPTURING"
	StateError       State = "ERROR"
)

// Command is pulled from the coordinator.
type Command struct {
	Type   string             `json:"type"`   // import | size_report | deploy | track | capture | browse
	Ref    string             `json:"ref"`    // for deploy/track
	Folder string             `json:"folder"` // for import/capture
	Apps   map[string]AppSpec `json:"apps"`   // for import + size_report (resolved specs)
	ReqID  string             `json:"req_id"` // for browse (correlates the result)
	Path   string             `json:"path"`   // for browse (dir to list; "" = drives)
}

// AppSpec mirrors one app entry from manifest.yaml. It is decoded both from the
// local manifest (yaml) and from coordinator commands (json), so it carries both
// tag sets. Exclude here is already merged with the manifest defaults by the
// coordinator before being sent to the agent.
type AppSpec struct {
	Live            []string `yaml:"live"              json:"live"`
	Repo            string   `yaml:"repo"              json:"repo"`
	Run             string   `yaml:"run"               json:"run"`
	StartDelay      int      `yaml:"start_delay"       json:"start_delay"`
	RestartOnChange bool     `yaml:"restart_on_change" json:"restart_on_change"`
	Exclude         []string `yaml:"exclude"           json:"exclude"`
}
