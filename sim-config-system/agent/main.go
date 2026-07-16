package main

// Sim config agent. Runs as a Windows service on each sim PC.
// Pull-based: polls the coordinator for commands, posts heartbeats + results,
// never pushes to git. Build: GOOS=windows go build -o simagent.exe .
//
// Phases: P1 import/size-report (read-only mirror UP). P2 deploy (fetch ->
// sparse checkout -> mirror worktree->live -> restart). P3 dev capture.

import (
	"log"
	"os"
	"sync"
	"time"
)

type Agent struct {
	cfg AgentConfig
	api *Client

	postUpdate bool // true when relaunched by a self-update — skip the boot redeploy

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
	for _, arg := range os.Args[1:] {
		if arg == "--post-update" {
			a.postUpdate = true // relaunched by a self-update: don't redeploy the sim
		}
	}
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
		log.Printf("waiting for coordinator instructions — %s not reachable yet, retrying…", cfg.CoordinatorURL)
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

	// HealthCheck sampling is independent of the state machine — a PC's temperature
	// history should keep building whether or not a load is deployed here.
	go a.runHealthLoop()

	log.Printf("agent %s (%s) v%s started in state %s", a.cfg.PCIP, a.cfg.Folder, Version, a.state)

	// FIRST: if the operator clicked "update", self-update + relaunch BEFORE any
	// sync, so the sync always runs on the latest agent.
	a.checkAndUpdate()

	// Boot-time: sync is ON-DEMAND ONLY. The agent NEVER resyncs or (re)launches on
	// start — for training OR dev loads. It adopts whatever load is live and just
	// reports its drift so PC status shows what changed; files/apps change only on an
	// explicit Deploy. (This preserves live edits across reboots.)
	if a.cfg.enforceOnStart() {
		cmd, err := a.api.GetEnforce()
		if err != nil || cmd == nil {
			// Coordinator unreachable, or it didn't name a live load — fall back to the
			// load we persisted the last time we adopted/deployed, so an already-deployed
			// PC still comes up as TRAINING/TESTING + drift instead of UNSEEDED.
			if cmd = a.loadSavedLoad(); cmd != nil && err != nil {
				log.Printf("enforce-on-start: %v — using last known load %s", err, cmd.Ref)
			}
		}
		if cmd != nil && repoCloned(a.cfg) {
			a.remember(*cmd, CheckClean(a.cfg, cmd.Apps))
			a.setState(StateTraining)
			log.Printf("startup: adopted %s — no resync (sync is on-demand only); reporting drift", cmd.Ref)
		} else if cmd != nil {
			log.Printf("startup: %s never deployed here — staying UNSEEDED until a Deploy", cmd.Ref)
		} else {
			log.Printf("startup: no live load known — staying UNSEEDED until a Deploy")
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
	case "filediff":
		a.doFileDiff(c) // read one drifted file (version vs live) for the UI diff
	case "install":
		a.doInstall(c) // provision a prerequisite (git bundle, redist, …)
	case "guard":
		a.doGuard(c) // run a per-PC compliance check/apply script
	case "update":
		a.doUpdate(c) // download new build, swap exe, relaunch
	case "shutdown":
		a.doShutdown(c) // PC status: force power off this PC
	case "health":
		a.sampleAndPost() // dashboard "refresh temperature": sample sensors now
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
		MAC: localMAC(a.cfg.PCIP), Host: localHost(),
	})
}

var (
	hostOnce sync.Once
	hostStr  string
)

// localHost is this PC's Windows computer name, reported on every heartbeat so the
// dashboard can show it next to the IP. Cached — it can't change without a reboot.
func localHost() string {
	hostOnce.Do(func() { hostStr, _ = os.Hostname() })
	return hostStr
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
	a.saveLoad(c.Ref, c.Apps) // persist so a restart can re-adopt without the coordinator
}
