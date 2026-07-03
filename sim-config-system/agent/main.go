package main

// Sim config agent. Runs as a Windows service on each sim PC.
// Pull-based: polls the coordinator for commands, posts heartbeats + results,
// never pushes to git. Build: GOOS=windows go build -o simagent.exe .
//
// Phases: P1 import/size-report (read-only mirror UP). P2 deploy (fetch ->
// sparse checkout -> mirror worktree->live -> restart). P3 dev capture.

import (
	"log"
	"sync"
	"time"
)

type Agent struct {
	cfg AgentConfig
	api *Client

	mu     sync.Mutex         // guards the fields below (read by heartbeat goroutine)
	state  State              //
	apps   map[string]AppSpec // app specs from the last deploy/track command
	ref    string             // the ref currently enforced (training-live / dev / tag)
	clean  bool               // cached drift status vs the deployed ref
	errMsg string             // last failure message (shown in the dashboard on ERROR)
}

func main() {
	cleanupOldBinary()              // remove leftover .old from a prior self-update
	cfg := LoadConfig("agent.json") // optional; baked defaults fill the rest
	resolveConfig(&cfg)
	a := &Agent{cfg: cfg, state: StateUnseeded, api: NewClient(cfg), clean: true}
	a.Run()
}

// resolveConfig fills anything agent.json didn't specify: coordinator + token from
// the baked-in build values, sane path defaults, and identity (pc_ip + folder)
// auto-detected from the coordinator via /whoami. Result: drop the exe on a PC and
// run it — no config file needed.
func resolveConfig(cfg *AgentConfig) {
	if cfg.CoordinatorURL == "" {
		cfg.CoordinatorURL = DefaultCoordinator
	}
	if cfg.Token == "" {
		cfg.Token = DefaultToken
	}
	if cfg.RepoPath == "" {
		cfg.RepoPath = "C:/sim-agent/repo"
	}
	// Leave GitExe empty unless explicitly set in agent.json — an empty value lets
	// gitExe() locate git (PATH, standard installs, or the portable Installs bundle).
	// Defaulting it to "git" here short-circuited all of that.
	if cfg.CoordinatorURL == "" {
		panic("no coordinator URL: provide agent.json or build with -ldflags -X main.DefaultCoordinator=...")
	}
	if cfg.PCIP != "" {
		return // explicit identity wins
	}
	// Auto-identity: ask the coordinator what IP it sees us as. Retry until reachable.
	for {
		ip, folder, err := NewClient(*cfg).Whoami()
		if err == nil && ip != "" {
			cfg.PCIP = ip
			if cfg.Folder == "" {
				cfg.Folder = folder
			}
			log.Printf("identity from coordinator: %s (folder=%q)", ip, cfg.Folder)
			return
		}
		log.Printf("waiting for coordinator at %s for identity: %v", cfg.CoordinatorURL, err)
		time.Sleep(3 * time.Second)
	}
}

func (a *Agent) Run() {
	hb := time.NewTicker(10 * time.Second)
	defer hb.Stop()

	go func() {
		n := 0
		for range hb.C {
			n++
			a.mu.Lock()
			st, apps := a.state, a.apps
			a.mu.Unlock()
			// Drift check is relatively heavy (stats live dirs), so only every ~60s.
			if st == StateTraining && n%6 == 0 {
				c := CheckClean(a.cfg, apps)
				a.mu.Lock()
				a.clean = c
				a.mu.Unlock()
			}
			a.heartbeat()
		}
	}()

	log.Printf("agent %s (%s) v%s started in state %s", a.cfg.PCIP, a.cfg.Folder, Version, a.state)

	// FIRST: if the operator clicked "update", self-update + relaunch BEFORE any
	// sync, so the sync always runs on the latest agent.
	a.checkAndUpdate()

	// Boot-time enforce: sync training-live and launch the apps BEFORE anything
	// else runs. This is what guarantees apps never start before a full sync —
	// the agent owns the launch, so remove the apps from Windows auto-start.
	if a.cfg.enforceOnStart() {
		if cmd, err := a.api.GetEnforce(); err != nil {
			log.Printf("enforce-on-start: %v (will rely on polled commands)", err)
		} else if cmd != nil {
			log.Printf("enforce-on-start: deploying %s", cmd.Ref)
			a.dispatch(*cmd)
		}
	}

	for {
		a.checkAndUpdate() // react to an "update" click on a running agent too
		cmd, err := a.api.PollCommand() // long-poll
		if err != nil {
			log.Printf("poll error: %v", err)
			time.Sleep(3 * time.Second)
			continue
		}
		if cmd == nil {
			continue
		}
		a.dispatch(*cmd)
	}
}

// checkAndUpdate self-updates (download new build, swap, relaunch) if the operator
// requested it. doUpdate acks + restarts, so this never returns when it fires.
func (a *Agent) checkAndUpdate() {
	pending, err := a.api.UpdatePending()
	if err != nil || !pending {
		return
	}
	log.Printf("[update] update requested — updating before sync")
	a.doUpdate(Command{})
}

func (a *Agent) dispatch(c Command) {
	switch c.Type {
	case "import":
		a.doImport(c) // bootstrap: mirror UP, read-only on this PC
	case "size_report":
		a.doSizeReport(c)
	case "deploy":
		a.doDeploy(c) // P2
	case "track":
		a.doTrack(c) // P3: switch to DEV_TRACKING on dev
	case "capture":
		a.doCapture(c) // P3: quiesce -> mirror live->worktree -> upload bundle
	case "browse":
		a.doBrowse(c) // config panel: list a dir/drives (read-only, no state change)
	case "drift":
		a.doDrift(c) // PC status: list files that differ from the deployed version
	case "install":
		a.doInstall(c) // provision a prerequisite (git bundle, redist, …)
	case "update":
		a.doUpdate(c) // download new build, swap exe, relaunch
	default:
		log.Printf("unknown command: %s", c.Type)
	}
}

func (a *Agent) heartbeat() {
	a.mu.Lock()
	st, ref, clean, errMsg := a.state, a.ref, a.clean, a.errMsg
	a.mu.Unlock()
	a.api.Heartbeat(Heartbeat{
		PCIP: a.cfg.PCIP, Folder: a.cfg.Folder,
		Mode: string(st), CurrentRef: ref, Clean: clean, Version: Version, Error: errMsg,
	})
}

// setState / remember keep the shared fields consistent under the lock. Leaving the
// ERROR state (a later command succeeds) clears the stored failure message.
func (a *Agent) setState(s State) {
	a.mu.Lock()
	a.state = s
	if s != StateError {
		a.errMsg = ""
	}
	a.mu.Unlock()
}

func (a *Agent) remember(c Command, clean bool) {
	a.mu.Lock()
	a.apps, a.ref, a.clean = c.Apps, c.Ref, clean
	a.mu.Unlock()
}
