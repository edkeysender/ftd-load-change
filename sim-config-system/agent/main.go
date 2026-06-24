package main

// Sim config agent. Runs as a Windows service on each sim PC.
// Pull-based: polls the coordinator for commands, posts heartbeats + results,
// never pushes to git. Build: GOOS=windows go build -o simagent.exe .
//
// Phase-1 scope (implement first): heartbeat, UNSEEDED state, import (read-only
// mirror UP to coordinator), size_report. deploy/track/capture follow in P2/P3.

import (
	"log"
	"time"
)

type Agent struct {
	cfg   AgentConfig
	state State
	api   *Client
}

func main() {
	cfg := LoadConfig("agent.yaml") // TODO: also accept Windows service args
	a := &Agent{cfg: cfg, state: StateUnseeded, api: NewClient(cfg)}
	a.Run()
}

func (a *Agent) Run() {
	hb := time.NewTicker(10 * time.Second)
	defer hb.Stop()

	go func() {
		for range hb.C {
			a.heartbeat()
		}
	}()

	log.Printf("agent %s (%s) started in state %s", a.cfg.PCIP, a.cfg.Folder, a.state)
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
	default:
		log.Printf("unknown command: %s", c.Type)
	}
}

func (a *Agent) heartbeat() {
	clean := true
	if a.state == StateTraining {
		clean = CheckClean(a.cfg) // TODO: compare worktree vs training-live
	}
	a.api.Heartbeat(Heartbeat{
		PCIP: a.cfg.PCIP, Folder: a.cfg.Folder,
		Mode: string(a.state), Clean: clean,
	})
}
