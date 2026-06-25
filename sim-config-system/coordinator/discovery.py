"""Network host discovery for the bootstrap/enrollment list.

Runs on the Pi (which sits on the sim LAN). Pure stdlib + system tools
(`ping`, `ip neigh`, optional `nmblookup`) — no extra Python deps. A ping sweep
finds live hosts; the ARP table gives MACs; reverse-DNS / NetBIOS gives names.

This only *discovers* machines and lets the operator curate a list. It does not
back anything up (the system is not a backup tool — see PROJECT_SPEC §14); the
list is an enrollment aid: which PCs are up, which already run an agent, which
are still missing one.
"""
import concurrent.futures
import ipaddress
import re
import shutil
import socket
import subprocess

MAX_HOSTS = 1024  # guard against accidentally scanning a huge range


def default_cidr(ips) -> str:
    """Derive a /24 from the first usable IPv4 in `ips` (the manifest PCs)."""
    for ip in ips:
        try:
            return str(ipaddress.ip_network(f"{ip}/24", strict=False))
        except ValueError:
            continue
    return "192.168.1.0/24"


def host_count(cidr: str) -> int:
    return ipaddress.ip_network(cidr, strict=False).num_addresses


def _ping(ip: str) -> bool:
    # Linux iputils: one packet, 1s timeout. Coordinator runs as root, so ping works.
    r = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def _arp_table() -> dict:
    """ip -> mac from the kernel neighbour table (populated by the ping sweep)."""
    macs = {}
    try:
        out = subprocess.run(["ip", "neigh"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            m = re.match(r"(\d+\.\d+\.\d+\.\d+).*lladdr ([0-9a-f:]{17})", line)
            if m:
                macs[m.group(1)] = m.group(2)
    except Exception:
        pass
    return macs


def _netbios(ip: str):
    """Windows NetBIOS name via nmblookup, if Samba tools are installed."""
    if not shutil.which("nmblookup"):
        return None
    try:
        out = subprocess.run(["nmblookup", "-A", ip], capture_output=True,
                             text=True, timeout=2).stdout
        for line in out.splitlines():
            m = re.search(r"^\s*(\S+)\s+<00>\s+UNIQUE", line)
            if m and m.group(1) != "MAC":
                return m.group(1)
    except Exception:
        pass
    return None


def _hostname(ip: str):
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(0.5)
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return _netbios(ip)
    finally:
        socket.setdefaulttimeout(old)


def scan(cidr: str, workers: int = 64) -> list:
    """Return [{ip, hostname, mac}] for every responsive host in `cidr`."""
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(h) for h in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        up = [ip for ip, ok in ex.map(lambda ip: (ip, _ping(ip)), hosts) if ok]
    macs = _arp_table()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        names = dict(ex.map(lambda ip: (ip, _hostname(ip)), up))
    return [{"ip": ip, "hostname": names.get(ip), "mac": macs.get(ip)} for ip in up]
