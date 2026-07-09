package main

// Power control from PC status: force shut down (agent-side) and the MAC lookup
// that lets the coordinator Wake-on-LAN this PC after it's powered off.

import (
	"log"
	"net"
	"os/exec"
	"sync"
)

// doShutdown forces this PC to power off immediately. Force (/f) so a running
// sim or locked apps can't veto it; /t 0 = now. The process dies with the box.
func (a *Agent) doShutdown(c Command) {
	log.Printf("[shutdown] force shutdown requested from coordinator")
	if err := exec.Command("shutdown", "/s", "/f", "/t", "0").Start(); err != nil {
		a.fail(err)
	}
}

var (
	macOnce sync.Once
	macStr  string
)

// localMAC returns the hardware address of the NIC that owns `ip`, reported on
// every heartbeat so the coordinator can wake this PC once it's off. Cached.
func localMAC(ip string) string {
	macOnce.Do(func() { macStr = lookupMAC(ip) })
	return macStr
}

func lookupMAC(ip string) string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, ifc := range ifaces {
		if len(ifc.HardwareAddr) != 6 {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, addr := range addrs {
			var a net.IP
			switch v := addr.(type) {
			case *net.IPNet:
				a = v.IP
			case *net.IPAddr:
				a = v.IP
			}
			if a != nil && a.String() == ip {
				return ifc.HardwareAddr.String()
			}
		}
	}
	return ""
}
