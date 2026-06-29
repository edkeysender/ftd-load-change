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

	mu    sync.Mutex          // guards the fields below (read by heartbeat goroutine)
	state State               //
	apps  map[string]AppSpec  // app specs from the last deploy/track command
	ref   string              // the ref currently enforced (training-live / dev / tag)
	clean bool                // cached drift status vs the deployed ref
}

func main() {
	cfg := LoadConfig("agent.json") // TODO: also accept Windows service args
	a := &Agent{cfg: cfg, state: StateUnseeded, api: NewClient(cfg), clean: true}
	a.Run()
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

	log.Printf("agent %s (%s) started in state %s", a.cfg.PCIP, a.cfg.Folder, a.state)

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
	default:
		log.Printf("unknown command: %s", c.Type)
	}
}

func (a *Agent) heartbeat() {
	a.mu.Lock()
	st, ref, clean := a.state, a.ref, a.clean
	a.mu.Unlock()
	a.api.Heartbeat(Heartbeat{
		PCIP: a.cfg.PCIP, Folder: a.cfg.Folder,
		Mode: string(st), CurrentRef: ref, Clean: clean,
	})
}

// setState / remember keep the shared fields consistent under the lock.
func (a *Agent) setState(s State) {
	a.mu.Lock()
	a.state = s
	a.mu.Unlock()
}

func (a *Agent) remember(c Command, clean bool) {
	a.mu.Lock()
	a.apps, a.ref, a.clean = c.Apps, c.Ref, clean
	a.mu.Unlock()
}
