package main

// Finding the coordinator, including after its address changes.
//
// The coordinator URL is baked into the binary at build time (see build-agent.sh), so
// a new Pi IP would otherwise strand the whole fleet: every agent would sit retrying an
// address that no longer exists — invisible in the dashboard and unable to self-update,
// since that path goes through the same dead URL. Instead the agent tries, in order:
//
//	1. agent.json         — an explicit operator override always wins
//	2. coordinator.json   — the last address that actually worked (written by the agent)
//	3. the baked default  — whatever build-agent.sh compiled in
//
// and if none of them answer: after a few rounds it sweeps its own /24 for a host that
// responds like a coordinator, and at any moment an operator at the console can press
// Enter and type a new address. Whatever finally answers is saved to coordinator.json,
// so the next start connects on the first try.
//
// Nothing is ever adopted without being verified against /whoami first — not a scan hit,
// not a typed-in address. A wrong entry costs a retry, never a wedged agent.

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	defaultCoordinatorPort = 8090

	probeTimeout     = 5 * time.Second // whole /whoami probe, incl. response
	probeDialTimeout = 2 * time.Second
	retryInterval    = 3 * time.Second
	promptTimeout    = 60 * time.Second

	roundsBeforeScan = 3               // failed rounds before the first subnet sweep
	rescanInterval   = 5 * time.Minute // a /24 sweep every 3s would be abusive
	scanBudget       = 30 * time.Second
	scanDialTimeout  = 400 * time.Millisecond
	scanWorkers      = 64
	maxScanTargets   = 1024 // same order of bound as MAX_HOSTS in coordinator/discovery.py
)

// probeHTTP is deliberately NOT the agent's normal client: that one carries a 10-minute
// timeout sized for import uploads (see NewClient), which would stall every probe for
// minutes against a host that is merely slow to refuse.
var probeHTTP = &http.Client{
	Timeout: probeTimeout,
	Transport: &http.Transport{
		DialContext:       (&net.Dialer{Timeout: probeDialTimeout}).DialContext,
		DisableKeepAlives: true,
	},
}

// ---- the last-known-good address ------------------------------------------

type savedCoordinator struct {
	CoordinatorURL string `json:"coordinator_url"`
}

// coordinatorFile sits next to sim-agent-load.json in the agent's own state dir. It is
// agent-owned: delete it to fall back to the baked-in build value.
func coordinatorFile(cfg AgentConfig) string {
	return filepath.Join(stateDir(cfg), "coordinator.json")
}

func loadSavedCoordinator(cfg AgentConfig) string {
	b, err := os.ReadFile(coordinatorFile(cfg))
	if err != nil {
		return ""
	}
	var s savedCoordinator
	if json.Unmarshal(b, &s) != nil {
		return ""
	}
	return s.CoordinatorURL
}

// saveCoordinator records an address that answered. Best-effort, and a no-op when it
// already matches, so a healthy agent never rewrites the file.
func saveCoordinator(cfg AgentConfig, base string) {
	if base == "" || base == loadSavedCoordinator(cfg) {
		return
	}
	b, err := json.Marshal(savedCoordinator{CoordinatorURL: base})
	if err != nil {
		return
	}
	// The state dir normally exists (-install creates it), but a hand-run agent from
	// another folder may get here first — and a lost address is the whole point.
	_ = os.MkdirAll(stateDir(cfg), 0o755)
	if err := os.WriteFile(coordinatorFile(cfg), b, 0o644); err != nil {
		log.Printf("warning: could not remember the coordinator address: %v", err)
		return
	}
	log.Printf("remembered coordinator %s in %s", base, coordinatorFile(cfg))
}

// coordinatorCandidates returns the addresses to try, best first, deduped. cfg's
// CoordinatorURL must still hold the raw agent.json value here (not the baked default),
// so an explicit override keeps its priority.
func coordinatorCandidates(cfg AgentConfig) []string {
	var out []string
	seen := map[string]bool{}
	for _, c := range []string{cfg.CoordinatorURL, loadSavedCoordinator(cfg), DefaultCoordinator} {
		n := normalizeCoordinator(c, defaultCoordinatorPort)
		if n == "" || seen[n] {
			continue
		}
		seen[n] = true
		out = append(out, n)
	}
	return out
}

// normalizeCoordinator turns anything an operator might type — "70.84.68.196",
// "70.84.68.196:8090", "http://pi:8090" — into a full base URL. Returns "" if it can't.
func normalizeCoordinator(in string, port int) string {
	s := strings.TrimSpace(in)
	s = strings.TrimSuffix(s, "/")
	if s == "" {
		return ""
	}
	if !strings.Contains(s, "://") {
		if h, p, err := net.SplitHostPort(s); err == nil && h != "" && p != "" {
			s = "http://" + s
		} else {
			s = fmt.Sprintf("http://%s:%d", s, port)
		}
	}
	u, err := url.Parse(s)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	if u.Port() == "" {
		u.Host = fmt.Sprintf("%s:%d", u.Host, port)
	}
	u.Path = strings.TrimSuffix(u.Path, "/")
	return u.String()
}

func coordinatorPort(base string) int {
	if u, err := url.Parse(base); err == nil {
		if p, err := strconv.Atoi(u.Port()); err == nil && p > 0 {
			return p
		}
	}
	return defaultCoordinatorPort
}

// ---- probing --------------------------------------------------------------

// probeWhoami is Whoami against an arbitrary base URL on the short-timeout client: the
// single test for "is a coordinator we can talk to at this address?". Used to vet every
// candidate, every scan hit and every typed-in address.
func probeWhoami(base, token string) (ip, folder string, err error) {
	path := base + "/whoami"
	if cands := localIPv4s(); len(cands) > 0 {
		path += "?candidates=" + url.QueryEscape(strings.Join(cands, ","))
	}
	req, err := http.NewRequest("GET", path, nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := probeHTTP.Do(req)
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
	if out.IP == "" {
		return "", "", fmt.Errorf("whoami returned no IP")
	}
	return out.IP, out.Folder, nil
}

// looksLikeCoordinator fingerprints a host WITHOUT sending our token, so a subnet sweep
// never sprays the shared secret at whatever else is listening on the LAN. An unauthorised
// /whoami on the real coordinator is a 401 "bad agent token" (see _auth in main.py); only
// a host that answers that way gets an authenticated probe.
func looksLikeCoordinator(ctx context.Context, base string) bool {
	req, err := http.NewRequestWithContext(ctx, "GET", base+"/whoami", nil)
	if err != nil {
		return false
	}
	resp, err := probeHTTP.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		return false
	}
	var body struct {
		Detail string `json:"detail"`
	}
	if json.NewDecoder(resp.Body).Decode(&body) != nil {
		return false
	}
	return strings.Contains(strings.ToLower(body.Detail), "agent token")
}

// ---- subnet sweep ---------------------------------------------------------

// scanTargets lists the host addresses on this PC's directly-connected IPv4 subnets,
// plus a label for the log line. Anything wider than a /24 is skipped: a /16 sweep is
// 65k connects, and a Hyper-V or VPN adapter would drag one in for nothing.
func scanTargets() (targets []string, label string) {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil, ""
	}
	self := map[string]bool{}
	for _, ip := range localIPv4s() {
		self[ip] = true
	}
	seen := map[string]bool{}
	var labels []string
	for _, a := range addrs {
		n, ok := a.(*net.IPNet)
		if !ok || n.IP.IsLoopback() {
			continue
		}
		v4 := n.IP.To4()
		if v4 == nil {
			continue
		}
		ones, bits := n.Mask.Size()
		if bits != 32 || ones < 24 || ones > 30 {
			continue // too wide to sweep politely, or too small to hold another host
		}
		base := v4.Mask(n.Mask).To4()
		size := 1 << uint(32-ones)
		labels = append(labels, fmt.Sprintf("%s/%d", base, ones))
		// ones >= 24, so only the last octet varies and base[3]+size-1 <= 255: plain
		// byte arithmetic, no carry to worry about.
		for i := 1; i < size-1 && len(targets) < maxScanTargets; i++ {
			ip := net.IPv4(base[0], base[1], base[2], base[3]+byte(i)).String()
			if seen[ip] || self[ip] {
				continue
			}
			seen[ip] = true
			targets = append(targets, ip)
		}
	}
	return targets, strings.Join(labels, ", ")
}

// scanForCoordinator sweeps the local subnet(s) for a coordinator on `port` and returns
// its base URL, or "" if none answered within the budget.
func scanForCoordinator(port int, token string) string {
	targets, label := scanTargets()
	if len(targets) == 0 {
		log.Printf("no directly-connected /24 to scan — skipping discovery")
		return ""
	}
	log.Printf("scanning %s for the coordinator on :%d (%d hosts)…", label, port, len(targets))

	ctx, cancel := context.WithTimeout(context.Background(), scanBudget)
	defer cancel()
	dialer := &net.Dialer{Timeout: scanDialTimeout}
	in := make(chan string)
	found := make(chan string, 1)

	var wg sync.WaitGroup
	for w := 0; w < scanWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for host := range in {
				if ctx.Err() != nil {
					return
				}
				conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(host, strconv.Itoa(port)))
				if err != nil {
					continue
				}
				conn.Close()
				base := fmt.Sprintf("http://%s:%d", host, port)
				if !looksLikeCoordinator(ctx, base) {
					continue
				}
				if _, _, err := probeWhoami(base, token); err != nil {
					log.Printf("%s answers like a coordinator but rejected us: %v", base, err)
					continue
				}
				select {
				case found <- base:
					cancel() // first verified hit wins; stop the rest
				default:
				}
				return
			}
		}()
	}
	go func() {
		defer close(in)
		for _, t := range targets {
			select {
			case in <- t:
			case <-ctx.Done():
				return
			}
		}
	}()
	wg.Wait()

	select {
	case base := <-found:
		log.Printf("found the coordinator at %s", base)
		return base
	default:
		log.Printf("scan finished — no coordinator answered on %s:%d", label, port)
		return ""
	}
}

// ---- console override -----------------------------------------------------

var (
	stdinOnce sync.Once
	stdinCh   chan string
)

// stdinLines yields lines typed at the console. A singleton: a second scanner on os.Stdin
// would steal lines from the first (startup resolution and a later recovery both want it).
// With no console — the agent normally runs as a scheduled task — the scan ends at once
// and the channel closes, which callers must treat as "no console" rather than spinning.
func stdinLines() <-chan string {
	stdinOnce.Do(func() {
		stdinCh = make(chan string)
		go func() {
			defer close(stdinCh)
			sc := bufio.NewScanner(os.Stdin)
			for sc.Scan() {
				stdinCh <- strings.TrimSpace(sc.Text())
			}
		}()
	})
	return stdinCh
}

// promptForCoordinator handles an operator breaking into the retry loop. `first` is the
// line they already typed: a bare Enter opens the prompt proper, anything else is taken
// as the address directly (so typing an IP and pressing Enter works in one go).
//
// Returns a verified base URL, or "" to keep the current address and go on retrying —
// on an empty answer, a 60s silence, an unparseable entry, or an address that doesn't
// answer. The agent never blocks here waiting for a human who isn't there.
func promptForCoordinator(first string, lines <-chan string, port int, token, current string) string {
	in := first
	if in == "" {
		fmt.Printf("coordinator address (host, host:port or full URL) — Enter alone keeps %s: ", current)
		select {
		case l, ok := <-lines:
			if !ok {
				fmt.Println()
				return ""
			}
			in = strings.TrimSpace(l)
		case <-time.After(promptTimeout):
			fmt.Println()
			log.Printf("nothing entered within %s — keeping %s", promptTimeout, current)
			return ""
		}
	}
	if in == "" {
		log.Printf("keeping %s", current)
		return ""
	}
	base := normalizeCoordinator(in, port)
	if base == "" {
		log.Printf("could not read %q as an address — keeping %s", in, current)
		return ""
	}
	log.Printf("checking %s…", base)
	if _, _, err := probeWhoami(base, token); err != nil {
		log.Printf("%s did not answer (%v) — keeping %s", base, err, current)
		return ""
	}
	log.Printf("%s answered — switching to it", base)
	return base
}

// ---- the resolution loop --------------------------------------------------

// resolveCoordinator blocks until some coordinator answers, then returns the identity it
// reported. It sets cfg.CoordinatorURL to the winning address and remembers it on disk.
// Candidates are tried best-first each round; the subnet sweep and the console prompt are
// the two ways a genuinely new address enters the list.
func resolveCoordinator(cfg *AgentConfig, cands []string) (ip, folder string) {
	lines := stdinLines()
	port := coordinatorPort(cands[0])
	var lastScan time.Time

	for round := 0; ; round++ {
		var lastErr error
		for _, base := range cands {
			gotIP, gotFolder, err := probeWhoami(base, cfg.Token)
			if err == nil {
				cfg.CoordinatorURL = base
				saveCoordinator(*cfg, base)
				return gotIP, gotFolder
			}
			lastErr = err
		}
		log.Printf("coordinator %s not reachable (%v) — retrying; press Enter to type a new address",
			cands[0], lastErr)

		// Sweep once we've clearly lost it, then only occasionally: the Pi may just be
		// rebooting, and re-scanning every 3s would hammer the LAN for nothing.
		if round+1 >= roundsBeforeScan && time.Since(lastScan) >= rescanInterval {
			lastScan = time.Now()
			if found := scanForCoordinator(port, cfg.Token); found != "" {
				cands = prependCoordinator(cands, found)
				continue // probe it right away
			}
		}

		deadline := time.After(retryInterval)
		for waiting := true; waiting; {
			select {
			case line, ok := <-lines:
				if !ok {
					lines = nil // no console: stop selecting on it (nil blocks forever)
					continue
				}
				if base := promptForCoordinator(line, lines, port, cfg.Token, cands[0]); base != "" {
					cands = prependCoordinator(cands, base)
				}
				waiting = false
			case <-deadline:
				waiting = false
			}
		}
	}
}

// prependCoordinator moves base to the front of the candidate list (adding it if new), so
// the next round tries the address we just learned about first.
func prependCoordinator(cands []string, base string) []string {
	out := []string{base}
	for _, c := range cands {
		if c != base {
			out = append(out, c)
		}
	}
	return out
}

// recoverCoordinator handles the coordinator moving under an already-running agent — the
// common case when the Pi picks up a new lease, since a rebooted PC would find it via the
// startup path anyway. Blocks until one answers: there is nothing useful to do until then.
//
// Identity (PCIP/Folder) is deliberately left alone — it is read by the heartbeat and
// health goroutines, and this PC's own address hasn't changed just because the Pi's did.
func (a *Agent) recoverCoordinator(down time.Duration) {
	current := a.api.Base()
	log.Printf("coordinator %s unreachable for %s — looking for it again", current, down.Round(time.Second))

	cfg := a.cfg // a copy: only CoordinatorURL is adopted back, below
	resolveCoordinator(&cfg, coordinatorCandidates(cfg))
	if cfg.CoordinatorURL == current {
		log.Printf("coordinator is back at %s", current)
		return
	}
	log.Printf("coordinator moved to %s (was %s) — reconnecting", cfg.CoordinatorURL, current)
	// Safe to write a.cfg here: CoordinatorURL is only read from this goroutine (the poll
	// loop and the dispatch/guard code it calls). The client's own base is mutex-guarded
	// because the heartbeat and health goroutines share it.
	a.cfg.CoordinatorURL = cfg.CoordinatorURL
	a.api.SetBase(cfg.CoordinatorURL)
}
