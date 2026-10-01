#!/usr/bin/env python3
"""
route_finder.py - find a VPS/proxy location that gives you a lower ping to a game server.

Why: your ISP's direct route to a server is sometimes much worse than the physical
distance suggests. A relay (VPS) in a well-connected city can beat it. This tool
estimates, for a catalog of VPS locations:

    total ping  ~=  (you -> VPS)  +  (VPS -> game server)

  * "you -> VPS" is measured from YOUR machine, by pinging each provider's public
    test host.
  * "VPS -> game server" is measured from machines INSIDE those providers' networks,
    using the free Globalping API (https://globalping.io), no account needed.

Needs only Python 3.8+ and the system `ping` command. No pip installs.

Usage
-----
  python route_finder.py scan  denver.quakeone.com --udp nq:26000
  python route_finder.py probe denver.quakeone.com --udp nq:26000 --count 30

  scan   rank VPS locations by estimated total ping (run this with NO tunnel/VPN on)
  probe  measure ICMP + game-protocol (UDP) round trip to the target right now.
         Run it with the relay off, then on, and compare.

Useful options for `scan`:
  --udp PROTO:PORT   also measure real game UDP latency to the target (nq, qw, q3)
  --countries US,CA  which countries' VPS locations to consider (default: US)
  --survey           also list the best cities/networks to reach the target from
  --hosts FILE.json  add your own VPS test hosts (see HOSTS FILE FORMAT below)
  --token TOKEN      Globalping API token (or env GLOBALPING_TOKEN) for higher limits
  --json FILE        save all results as JSON

Globalping quotas vary; the tool checks the API for your current allowance.
`--budget` caps requested probes across the scan and optional survey.

Caveats (important):
  * Estimates are ICMP-based. Game UDP can take a different path, so confirm with
    `probe` after you deploy something.
  * Two VPS in the SAME city/provider can route differently (we saw 32 ms vs 52 ms
    on two Vultr Los Angeles machines). Deploy the cheapest plan, run `probe`, and
    delete it if it is no better than direct. Hourly billing makes that cheap.
  * The game server will see the VPS's IP address, not yours.

HOSTS FILE FORMAT (--hosts): a JSON list, e.g.
  [{"provider": "MyHost", "name": "MyHost Denver", "host": "1.2.3.4",
    "country": "US", "cities": ["Denver"], "asns": [12345]}]
  "cities" and "asns" are how Globalping probes are matched to your entry.
"""

import argparse
import concurrent.futures as cf
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

GLOBALPING = "https://api.globalping.io/v1/measurements"

# provider -> ASNs used to find that provider's probes in Globalping
PROVIDER_ASNS = {
    "Vultr": [20473],
    "Linode": [63949],
    "Clouvider": [62240],
    "Leaseweb": [7203, 395954, 30633],  # US; other countries override below
    "Hetzner": [24940],
    "FiberState": [26042],
    "Shock Hosting": [395092],
    "RackNerd": [36352],      # ColoCrossing network (also used by many resellers)
    "ColoCrossing": [36352],
    "HostHatch": [63473],
    "OVH": [16276],
    "BuyVM": [53667],
    "IONOS": [8560],
    "Limestone": [46475],
    "AWS": [16509],
    "Oracle": [31898],
}


LEASEWEB_OUTSIDE_US = {"DE": [28753], "NL": [60781, 16265], "SG": [59253], "AU": [59253]}


def _e(provider, name, host, country, cities, tcp=None):
    asns = PROVIDER_ASNS[provider]
    if provider == "Leaseweb" and country in LEASEWEB_OUTSIDE_US:
        asns = LEASEWEB_OUTSIDE_US[country]
    entry = {"provider": provider, "name": name, "host": host, "country": country,
             "cities": cities, "asns": asns}
    if tcp:
        entry["tcp"] = tcp  # time a TCP connect instead of ICMP (host may not answer ping)
    return entry


# Public ping/test hosts. All of these resolved and answered ICMP when this was written.
CATALOG = [
    # --- United States ---
    _e("Vultr", "Vultr Los Angeles", "lax-ca-us-ping.vultr.com", "US", ["Los Angeles"]),
    _e("Vultr", "Vultr Silicon Valley", "sjo-ca-us-ping.vultr.com", "US", ["Santa Clara", "San Jose"]),
    _e("Vultr", "Vultr Seattle", "wa-us-ping.vultr.com", "US", ["Seattle"]),
    _e("Vultr", "Vultr Dallas", "tx-us-ping.vultr.com", "US", ["Dallas"]),
    _e("Vultr", "Vultr Chicago", "il-us-ping.vultr.com", "US", ["Naperville", "Chicago"]),
    _e("Vultr", "Vultr New Jersey", "nj-us-ping.vultr.com", "US", ["Piscataway", "Newark"]),
    _e("Vultr", "Vultr Atlanta", "ga-us-ping.vultr.com", "US", ["Atlanta"]),
    _e("Vultr", "Vultr Miami", "fl-us-ping.vultr.com", "US", ["Miami"]),
    _e("Linode", "Linode Fremont", "speedtest.fremont.linode.com", "US", ["Fremont", "San Jose"]),
    _e("Linode", "Linode Dallas", "speedtest.dallas.linode.com", "US", ["Dallas"]),
    _e("Linode", "Linode Chicago", "speedtest.chicago.linode.com", "US", ["Chicago"]),
    _e("Linode", "Linode Seattle", "speedtest.seattle.linode.com", "US", ["Seattle"]),
    _e("Linode", "Linode Atlanta", "speedtest.atlanta.linode.com", "US", ["Atlanta"]),
    _e("Linode", "Linode Newark", "speedtest.newark.linode.com", "US", ["Newark"]),
    _e("Linode", "Linode Miami", "speedtest.miami.linode.com", "US", ["Miami"]),
    _e("Clouvider", "Clouvider Los Angeles", "la.speedtest.clouvider.net", "US", ["Los Angeles"]),
    _e("Clouvider", "Clouvider Phoenix", "phx.speedtest.clouvider.net", "US", ["Phoenix"]),
    _e("Clouvider", "Clouvider Dallas", "dal.speedtest.clouvider.net", "US", ["Dallas"]),
    _e("Clouvider", "Clouvider Chicago", "chi.speedtest.clouvider.net", "US", ["Chicago"]),
    _e("Clouvider", "Clouvider New York", "nyc.speedtest.clouvider.net", "US", ["New York"]),
    _e("Clouvider", "Clouvider Ashburn", "ash.speedtest.clouvider.net", "US", ["Ashburn"]),
    _e("Leaseweb", "Leaseweb San Francisco", "mirror.sfo12.us.leaseweb.net", "US", ["San Jose", "San Francisco"]),
    _e("Leaseweb", "Leaseweb Dallas", "mirror.dal10.us.leaseweb.net", "US", ["Dallas"]),
    _e("Leaseweb", "Leaseweb Washington DC", "mirror.wdc1.us.leaseweb.net", "US", ["Ashburn", "Washington"]),
    _e("Hetzner", "Hetzner Hillsboro", "hil-speed.hetzner.com", "US", ["Hillsboro", "Portland"]),
    _e("Hetzner", "Hetzner Ashburn", "ash-speed.hetzner.com", "US", ["Ashburn"]),
    _e("FiberState", "FiberState Los Angeles", "lax.lg.fiberstate.com", "US", ["Los Angeles"]),
    _e("FiberState", "FiberState Salt Lake City", "23.145.120.2", "US", ["Salt Lake City"]),
    _e("Shock Hosting", "Shock Hosting Denver", "216.120.203.3", "US", ["Denver"]),
    _e("RackNerd", "RackNerd Los Angeles", "107.174.51.158", "US", ["Los Angeles"]),
    _e("RackNerd", "RackNerd San Jose", "192.210.207.88", "US", ["San Jose"]),
    _e("RackNerd", "RackNerd Seattle", "192.3.253.2", "US", ["Seattle"]),
    _e("RackNerd", "RackNerd Dallas", "198.23.249.100", "US", ["Dallas"]),
    _e("RackNerd", "RackNerd Chicago", "198.23.228.15", "US", ["Chicago"]),
    _e("ColoCrossing", "ColoCrossing Dallas", "192.3.237.150", "US", ["Dallas"]),
    _e("HostHatch", "HostHatch Los Angeles", "lg.lax.hosthatch.com", "US", ["Los Angeles"]),
    _e("HostHatch", "HostHatch Chicago", "lg.chi.hosthatch.com", "US", ["Chicago"]),
    _e("OVH", "OVH Vint Hill (VA)", "was1-vin.speedtest.network.ovh.net", "US", ["Vint Hill", "Warrenton"]),
    _e("BuyVM", "BuyVM New York", "198.98.53.31", "US", ["New York"]),
    _e("BuyVM", "BuyVM Miami", "45.61.191.250", "US", ["Miami"]),
    _e("IONOS", "IONOS Kansas City", "74.208.236.7", "US", ["Kansas City"]),
    _e("Limestone", "Limestone Salt Lake City", "74.63.204.218", "US", ["Salt Lake City"]),
    # Big clouds. These answer TCP 443 but not always ping, so we time a TCP connect.
    _e("AWS", "AWS us-west-1 (N. California)", "dynamodb.us-west-1.amazonaws.com", "US", ["San Jose", "San Francisco"], tcp=443),
    _e("AWS", "AWS us-west-2 (Oregon)", "dynamodb.us-west-2.amazonaws.com", "US", ["Portland", "Boardman", "Hillsboro"], tcp=443),
    _e("AWS", "AWS us-east-1 (Virginia)", "dynamodb.us-east-1.amazonaws.com", "US", ["Ashburn"], tcp=443),
    _e("AWS", "AWS us-east-2 (Ohio)", "dynamodb.us-east-2.amazonaws.com", "US", ["Columbus"], tcp=443),
    _e("Oracle", "Oracle San Jose (free tier)", "objectstorage.us-sanjose-1.oraclecloud.com", "US", ["San Jose"], tcp=443),
    _e("Oracle", "Oracle Phoenix (free tier)", "objectstorage.us-phoenix-1.oraclecloud.com", "US", ["Phoenix"], tcp=443),
    _e("Oracle", "Oracle Chicago (free tier)", "objectstorage.us-chicago-1.oraclecloud.com", "US", ["Chicago"], tcp=443),
    _e("Oracle", "Oracle Ashburn (free tier)", "objectstorage.us-ashburn-1.oraclecloud.com", "US", ["Ashburn"], tcp=443),
    # --- Canada ---
    _e("OVH", "OVH Beauharnois (QC)", "ymq1-bhs.speedtest.network.ovh.net", "CA", ["Beauharnois", "Montreal"]),
    _e("AWS", "AWS ca-central-1 (Montreal)", "dynamodb.ca-central-1.amazonaws.com", "CA", ["Montreal"], tcp=443),
    _e("Vultr", "Vultr Toronto", "tor-ca-ping.vultr.com", "CA", ["Toronto"]),
    _e("Linode", "Linode Toronto", "speedtest.toronto1.linode.com", "CA", ["Toronto"]),
    # --- Europe ---
    _e("Vultr", "Vultr London", "lon-gb-ping.vultr.com", "GB", ["London"]),
    _e("Vultr", "Vultr Amsterdam", "ams-nl-ping.vultr.com", "NL", ["Amsterdam"]),
    _e("Vultr", "Vultr Paris", "par-fr-ping.vultr.com", "FR", ["Paris"]),
    _e("Vultr", "Vultr Frankfurt", "fra-de-ping.vultr.com", "DE", ["Frankfurt"]),
    _e("Linode", "Linode London", "speedtest.london.linode.com", "GB", ["London"]),
    _e("Linode", "Linode Frankfurt", "speedtest.frankfurt.linode.com", "DE", ["Frankfurt"]),
    _e("Hetzner", "Hetzner Falkenstein", "fsn1-speed.hetzner.com", "DE", ["Falkenstein"]),
    _e("Hetzner", "Hetzner Nuremberg", "nbg1-speed.hetzner.com", "DE", ["Nuremberg"]),
    _e("Hetzner", "Hetzner Helsinki", "hel1-speed.hetzner.com", "FI", ["Helsinki"]),
    _e("Leaseweb", "Leaseweb Frankfurt", "mirror.fra10.de.leaseweb.net", "DE", ["Frankfurt"]),
    _e("Leaseweb", "Leaseweb Amsterdam", "mirror.ams1.nl.leaseweb.net", "NL", ["Amsterdam"]),
    _e("AWS", "AWS eu-west-1 (Ireland)", "dynamodb.eu-west-1.amazonaws.com", "IE", ["Dublin"], tcp=443),
    _e("AWS", "AWS eu-central-1 (Frankfurt)", "dynamodb.eu-central-1.amazonaws.com", "DE", ["Frankfurt"], tcp=443),
    # --- Asia / Pacific / South America ---
    _e("AWS", "AWS ap-southeast-1 (Singapore)", "dynamodb.ap-southeast-1.amazonaws.com", "SG", ["Singapore"], tcp=443),
    _e("AWS", "AWS ap-northeast-1 (Tokyo)", "dynamodb.ap-northeast-1.amazonaws.com", "JP", ["Tokyo"], tcp=443),
    _e("AWS", "AWS ap-southeast-2 (Sydney)", "dynamodb.ap-southeast-2.amazonaws.com", "AU", ["Sydney"], tcp=443),
    _e("AWS", "AWS sa-east-1 (Sao Paulo)", "dynamodb.sa-east-1.amazonaws.com", "BR", ["Sao Paulo"], tcp=443),
    _e("Vultr", "Vultr Singapore", "sgp-ping.vultr.com", "SG", ["Singapore"]),
    _e("Vultr", "Vultr Sydney", "syd-au-ping.vultr.com", "AU", ["Sydney"]),
    _e("Linode", "Linode Singapore", "speedtest.singapore.linode.com", "SG", ["Singapore"]),
    _e("Linode", "Linode Tokyo", "speedtest.tokyo2.linode.com", "JP", ["Tokyo"]),
    _e("Linode", "Linode Sydney", "speedtest.syd1.linode.com", "AU", ["Sydney"]),
    _e("Hetzner", "Hetzner Singapore", "sin-speed.hetzner.com", "SG", ["Singapore"]),
    _e("Leaseweb", "Leaseweb Singapore", "mirror.sin1.sg.leaseweb.net", "SG", ["Singapore"]),
    _e("Leaseweb", "Leaseweb Sydney", "mirror.syd10.au.leaseweb.net", "AU", ["Sydney"]),
]

DEFAULT_SURVEY_CITIES = {
    "US": ["Los Angeles", "San Jose", "Seattle", "Salt Lake City", "Denver", "Phoenix",
           "Dallas", "Kansas City", "Chicago", "Atlanta", "Ashburn", "New York", "Miami"],
}

UDP_PAYLOADS = {
    # NetQuake / QSS-M (default port 26000): CCREQ_SERVER_INFO
    "nq": b"\x80\x00\x00\x0c\x02QUAKE\x00\x03",
    # QuakeWorld / FTE / ezQuake servers (default port 27500)
    "qw": b"\xff\xff\xff\xffstatus\n",
    # Quake 3 / ioquake3 and relatives (default port 27960)
    "q3": b"\xff\xff\xff\xffgetstatus\n",
}


# --------------------------------------------------------------------------- helpers
def log(msg=""):
    print(msg, flush=True)


def resolve(host):
    try:
        return socket.gethostbyname(host)
    except OSError as exc:
        sys.exit(f"Cannot resolve {host!r}: {exc}")


def ping(host, count=5, timeout=2):
    """ICMP ping via the system ping command. Returns dict or None if no replies."""
    system = platform.system()
    if system == "Windows":
        cmd = ["ping", "-n", str(count), "-w", str(timeout * 1000), host]
    elif system == "Darwin":
        cmd = ["ping", "-c", str(count), "-W", str(timeout * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(timeout), host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=count * (timeout + 1) + 5).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    times = [float(x) for x in re.findall(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)]
    if not times:
        return None
    return {"min": min(times), "avg": statistics.mean(times), "max": max(times),
            "loss": 100.0 * (count - len(times)) / count, "n": len(times)}


def tcp_ping(host, port, count=6, timeout=2.0):
    """Time TCP connects (works for hosts that block ICMP)."""
    times = []
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.1)
    if not times:
        return None
    return {"min": min(times), "avg": statistics.mean(times), "max": max(times),
            "loss": 100.0 * (count - len(times)) / count, "n": len(times)}


def measure_entry(entry):
    if entry.get("tcp"):
        return tcp_ping(entry["host"], entry["tcp"])
    return ping(entry["host"], count=5)


def udp_probe(ip, spec, count=25, timeout=1.0):
    """Round-trip a real game query over UDP. spec like 'nq:26000'."""
    spec = validate_udp_spec(spec)
    proto, port = spec.split(":")
    payload = UDP_PAYLOADS[proto]
    times = []
    for _ in range(count):
        # Connected sockets filter other senders. A fresh socket for each query
        # prevents late replies to earlier queries contaminating the next sample.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, int(port)))
            t0 = time.perf_counter()
            try:
                sock.send(payload)
                if sock.recv(4096):
                    times.append((time.perf_counter() - t0) * 1000)
            except OSError:
                pass
        time.sleep(0.15)
    if not times:
        return None
    return {"min": min(times), "avg": statistics.mean(times), "max": max(times),
            "loss": 100.0 * (count - len(times)) / count, "n": len(times)}


def validate_udp_spec(spec):
    try:
        proto, port = spec.lower().split(":")
        port = int(port)
        if proto not in UDP_PAYLOADS or not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("UDP must be nq:PORT, qw:PORT, or q3:PORT (port 1–65535)")
    return f"{proto}:{port}"


def fmt(stats):
    if not stats:
        return "no reply"
    return f"avg {stats['avg']:.0f} ms (best {stats['min']:.0f}, worst {stats['max']:.0f}, loss {stats.get('loss', 0):.0f}%, n={stats['n']})"


# --------------------------------------------------------------------------- Globalping
class RateLimited(Exception):
    pass


def globalping(body, token=None, wait=60):
    headers = {"Content-Type": "application/json", "User-Agent": "route-finder/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(GLOBALPING, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            mid = json.load(resp)["id"]
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited()
        try:
            err = json.loads(exc.read().decode(errors="replace")).get("error", {})
        except ValueError:
            err = {}
        if err.get("type") == "no_probes_found":
            return {"results": []}
        raise RuntimeError(f"Globalping HTTP {exc.code}: {err.get('message', 'error')}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Globalping unreachable: {exc}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(2)
        poll = urllib.request.Request(f"{GLOBALPING}/{mid}", headers={"User-Agent": "route-finder/1.0"})
        try:
            with urllib.request.urlopen(poll, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited("Globalping rate limit reached") from exc
            raise RuntimeError(f"Globalping polling HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"Globalping polling failed: {exc}") from exc
        if data.get("status") != "in-progress":
            return data
    raise RuntimeError(f"Globalping measurement {mid} did not finish within {wait} seconds")


def gp_limits(token=None):
    """(remaining_tests, seconds_until_reset) from Globalping, or None if unavailable."""
    headers = {"User-Agent": "route-finder/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(GLOBALPING.replace("/measurements", "/limits"), headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            c = json.load(resp)["rateLimit"]["measurements"]["create"]
        return int(c["remaining"]), int(c["reset"])
    except (urllib.error.URLError, KeyError, ValueError, TypeError):
        return None


def globalping_chunked(target, locs, token=None):
    """Globalping allows at most 50 probes per measurement; split the locations to fit."""
    chunks, cur, total = [], [], 0
    for loc in locs:
        if cur and total + loc["limit"] > 50:
            chunks.append(cur)
            cur, total = [], 0
        cur.append(loc)
        total += loc["limit"]
    if cur:
        chunks.append(cur)
    rows = []
    for chunk in chunks:
        body = {"type": "ping", "target": target, "locations": chunk, "measurementOptions": {"packets": 4}}
        rows += probe_rows(globalping(body, token=token))
    return rows


def probe_rows(data):
    rows = []
    for r in data.get("results", []):
        stats = (r.get("result") or {}).get("stats") or {}
        if stats.get("avg") is None:
            continue
        p = r["probe"]
        rows.append({"city": p.get("city") or "?", "country": p.get("country"), "asn": p.get("asn"),
                     "network": p.get("network"), "min": stats["min"], "avg": stats["avg"],
                     "loss": stats.get("loss", 0)})
    return rows


# --------------------------------------------------------------------------- commands
def cmd_probe(args):
    ip = resolve(args.target)
    log(f"Target {args.target} ({ip})")
    log(f"  ICMP ping : {fmt(ping(ip, count=args.count))}")
    if args.udp:
        log(f"  UDP {args.udp:<9}: {fmt(udp_probe(ip, args.udp, count=args.count))}")
    log("\nRun this with your relay/VPN off, then on, and compare the averages AND the 'worst' values.")


def cmd_scan(args):
    ip = resolve(args.target)
    countries = {c.strip().upper() for c in args.countries.split(",")}
    catalog = [dict(e) for e in CATALOG]
    if args.hosts:
        with open(args.hosts, encoding="utf-8") as fh:
            extra = json.load(fh)
        if not isinstance(extra, list):
            raise ValueError("hosts file must contain a JSON list")
        for i, entry in enumerate(extra, 1):
            if not isinstance(entry, dict):
                raise ValueError(f"hosts entry {i} must be an object")
            for field in ("provider", "name", "host", "country"):
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    raise ValueError(f"hosts entry {i}: {field} must be a nonempty string")
            if entry["host"].startswith("-"):
                raise ValueError(f"hosts entry {i}: host cannot start with '-'")
            if not re.fullmatch(r"[A-Za-z]{2}", entry["country"]):
                raise ValueError(f"hosts entry {i}: country must be a two-letter code")
            entry["country"] = entry["country"].upper()
            cities, asns = entry.get("cities"), entry.get("asns")
            if not isinstance(cities, list) or not cities or any(
                    not isinstance(c, str) or not c.strip() for c in cities):
                raise ValueError(f"hosts entry {i}: cities must be a nonempty list of strings")
            if not isinstance(asns, list) or not asns or any(
                    type(a) is not int or not 1 <= a <= 4294967295 for a in asns):
                raise ValueError(f"hosts entry {i}: asns must be a nonempty list of positive ASN integers")
            if "tcp" in entry and (type(entry["tcp"]) is not int or not 1 <= entry["tcp"] <= 65535):
                raise ValueError(f"hosts entry {i}: tcp must be a port from 1 to 65535")
        catalog += extra
    catalog = [e for e in catalog if e["country"] in countries]
    if not catalog:
        sys.exit("No catalog entries for those countries. Add some with --hosts.")

    log(f"Target: {args.target} ({ip})")
    log("Make sure no VPN / tunnel / game-booster is running, or the baseline will be wrong.\n")

    # 1. baseline
    log("[1/4] Measuring your direct route ...")
    direct = ping(ip, count=10)
    log(f"      ICMP : {fmt(direct)}")
    direct_udp = None
    if args.udp:
        direct_udp = udp_probe(ip, args.udp, count=20)
        log(f"      UDP  : {fmt(direct_udp)}")
    if not direct:
        log("      (target does not answer ping; savings will be shown against nothing)")
    baseline = direct["avg"] if direct else None

    # 2. leg 1 from your machine
    log(f"\n[2/4] Pinging {len(catalog)} VPS test hosts from your machine ...")
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        leg1 = list(pool.map(measure_entry, catalog))
    for e, s in zip(catalog, leg1):
        e["leg1"] = s
    reachable = [e for e in catalog if e["leg1"]]
    log(f"      {len(reachable)}/{len(catalog)} answered ping")
    # a relay can never beat direct if just reaching it already costs more than direct
    if baseline is not None:
        candidates = [e for e in reachable if e["leg1"]["avg"] < baseline]
    else:
        candidates = reachable
    log(f"      {len(candidates)} are closer to you than the target is (the rest cannot help)")

    # 3. leg 2: ping the target from inside each (network, city) that matters, via Globalping
    want = {}  # (asn, country, city) -> lowest you->VPS ping among entries that need it
    for e in candidates:
        for asn in e["asns"]:
            for city in e["cities"]:
                key = (asn, e["country"], city)
                want[key] = min(want.get(key, 1e9), e["leg1"]["avg"])
    per = max(1, min(args.probes, 10))
    budget = args.budget
    lim = gp_limits(args.token)
    if lim:
        remaining, reset = lim
        log(f"\n      Globalping tests remaining: {remaining} (resets in {reset // 60} min)")
        budget = min(budget, remaining)
    ordered = sorted(want, key=lambda k: want[k])  # most promising (closest to you) first
    log(f"\n[3/4] Asking Globalping to ping the target from {len(ordered)} (network, city) spots, "
        f"{per} probes each, budget {budget} tests ...")

    lock = threading.Lock()
    state = {"spent": 0, "rate": False}

    def work(key):
        asn, country, city = key
        with lock:
            if state["rate"] or state["spent"] + per > budget:
                return key, None
            # Reserve before releasing the lock, including requests without stats.
            state["spent"] += per
        try:
            rows = globalping_chunked(ip, [{"country": country, "city": city, "asn": asn, "limit": per}],
                                      token=args.token)
        except RateLimited:
            with lock:
                state["rate"] = True
            return key, None
        except RuntimeError as exc:
            log(f"      {city} (AS{asn}): {exc}")
            return key, None
        return key, rows

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        probes = dict(pool.map(work, ordered))  # key -> rows, or None if not measured
    rate_limited = state["rate"]
    hits = sum(1 for v in probes.values() if v)
    skipped = sum(1 for v in probes.values() if v is None)
    log(f"      {hits} spots had probes, reserved {state['spent']} tests")
    if rate_limited:
        log("      Globalping rate limit reached.")
        log("      Wait for the reset or pass --token (free at https://globalping.io). Showing partial results.")
    elif skipped:
        log(f"      {skipped} spots NOT measured (out of budget). Rerun after the reset, raise --budget, "
            "or use a free --token.")

    # 4. combine
    results = []
    for e in candidates:
        keys = [(asn, e["country"], c) for asn in e["asns"] for c in e["cities"]]
        match = [r for k in keys for r in (probes.get(k) or [])]
        leg2 = None
        if match:
            avgs = [r["avg"] for r in match]
            leg2 = {"med": statistics.median(avgs), "min": min(avgs), "max": max(avgs), "n": len(avgs)}
        measured = any(probes.get(k) is not None for k in keys)
        est = e["leg1"]["avg"] + leg2["med"] if leg2 else None
        worst = e["leg1"]["avg"] + leg2["max"] if leg2 else None
        results.append({"name": e["name"], "host": e["host"], "leg1": e["leg1"], "leg2": leg2,
                        "estimate": est, "estimate_worst": worst, "measured": measured})
    results.sort(key=lambda r: (r["estimate"] is None, r["estimate"] or 0, r["leg1"]["avg"]))

    log("\n[4/4] Results (lower is better)\n")
    head = f"{'#':>2}  {'VPS location':<28}{'you->VPS':>9}{'VPS->target':>13}{'range':>12}{'EST. TOTAL':>12}{'vs direct':>11}"
    log(head)
    log("-" * len(head))
    if direct:
        log(f"{'':>2}  {'(direct, no relay)':<28}{'':>9}{'':>13}{'':>12}{baseline:>11.0f}ms{'':>9}")
    shown = 0
    for r in results:
        if r["estimate"] is None:
            continue
        shown += 1
        rng = f"{r['leg2']['min']:.0f}-{r['leg2']['max']:.0f}"
        delta = f"{r['estimate'] - baseline:+.0f}ms" if baseline is not None else ""
        warn = "  <- varies, verify" if r["leg2"]["max"] - r["leg2"]["min"] > 8 else ""
        log(f"{shown:>2}  {r['name']:<28}{r['leg1']['avg']:>7.0f}ms{r['leg2']['med']:>11.0f}ms{rng:>12}"
            f"{r['estimate']:>10.0f}ms{delta:>11}{warn}")
    not_measured = [r for r in results if r["estimate"] is None and not r["measured"]]
    no_probe = [r for r in results if r["estimate"] is None and r["measured"]]
    if not_measured:
        log("\nNot measured yet (Globalping budget/rate limit): " + ", ".join(r["name"] for r in not_measured))
        log("These are close enough to you to be worth checking. Rerun later or raise --budget.")
    if no_probe:
        log("\nNo matching Globalping probe in that city: " + ", ".join(r["name"] for r in no_probe))
        log("(you->VPS was measured, but the VPS->target leg is unknown; fix 'cities'/'asns' via --hosts)")

    best = next((r for r in results if r["estimate"] is not None), None)
    if best and baseline is not None and best["estimate"] < baseline - 3:
        log(f"\nBest estimate: {best['name']}  ~{best['estimate']:.0f} ms vs {baseline:.0f} ms direct "
            f"(saves ~{baseline - best['estimate']:.0f} ms).")
        log("Next: rent the cheapest hourly VPS there, forward only the game server's IP through it "
            "(e.g. WireGuard), then confirm:")
        log(f"      python route_finder.py probe {args.target}" + (f" --udp {args.udp}" if args.udp else ""))
        log("If it is not better than direct, destroy the VPS and try the next row.")
        log("Check that the provider actually rents VPS (ideally hourly) in that city; some rows are "
            "datacenter/network operators, so you may need to pick a similar reseller there.")
    elif best:
        log("\nNo location is clearly better than your direct route. A relay probably will not help here.")
    elif not rate_limited:
        log("\nNo usable results. Try --countries with other countries, or add hosts with --hosts.")

    survey = None
    if args.survey:
        survey = run_survey(ip, countries, args, budget=budget - state["spent"])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"target": args.target, "ip": ip, "direct_icmp": direct, "direct_udp": direct_udp,
                       "results": results, "survey": survey}, fh, indent=2)
        log(f"\nSaved {args.json}")


def run_survey(ip, countries, args, budget=None):
    log("\n--- Survey: where on the map is the target reachable fastest? (any network) ---")
    if args.cities:
        spots = [(country, city.strip()) for country in sorted(countries)
                 for city in args.cities.split(",") if city.strip()]
    else:
        spots = [(country, city) for country in sorted(countries)
                 for city in DEFAULT_SURVEY_CITIES.get(country, [])]
    if not spots:
        log("Pass --cities for this country.")
        return None
    remaining = args.budget if budget is None else budget
    locs = []
    for country, city in spots:
        if remaining <= 0:
            break
        limit = min(5, remaining)
        locs.append({"country": country, "city": city, "limit": limit})
        remaining -= limit
    if not locs:
        log("Survey skipped: no Globalping budget remaining.")
        return None
    if len(locs) < len(spots):
        log("Survey limited to remaining scan budget.")
    try:
        rows = globalping_chunked(ip, locs, token=args.token)
    except (RateLimited, RuntimeError) as exc:
        log(f"Survey skipped: {exc or 'rate limited'}")
        return None
    by_city = {}
    for r in rows:
        by_city.setdefault(r["city"], []).append(r)
    table = []
    for city, rs in by_city.items():
        best = min(rs, key=lambda r: r["avg"])
        table.append((best["avg"], city, best["network"], statistics.median(r["avg"] for r in rs), len(rs)))
    table.sort()
    log(f"{'city':<18}{'best':>8}{'median':>9}  best network")
    for best, city, net, med, n in table:
        log(f"{city:<18}{best:>6.0f}ms{med:>7.0f}ms  {net} ({n} probes)")
    log("Cities with a low number here are good places for a relay, but you also need a low ping from "
        "YOU to that city - check your own route to hosts there.")
    return [{"city": c, "best": b, "median": m, "network": n} for b, c, n, m, _ in table]


def main():
    ap = argparse.ArgumentParser(description="Find a VPS location that lowers your ping to a game server.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="rank VPS locations by estimated total ping")
    sp.add_argument("target", help="game server hostname or IP")
    sp.add_argument("--udp", type=validate_udp_spec, help="measure real game UDP too, e.g. nq:26000, qw:27500, q3:27960")
    sp.add_argument("--countries", default="US", help="comma-separated country codes (default US)")
    sp.add_argument("--probes", type=int, default=3,
                    help="Globalping probes per (network, city) spot, max 10 (default 3)")
    sp.add_argument("--budget", type=int, default=150,
                    help="max requested Globalping probes, including survey (default 150)")
    sp.add_argument("--survey", action="store_true", help="also show best cities to reach the target from")
    sp.add_argument("--cities", help="comma-separated cities for --survey")
    sp.add_argument("--hosts", help="JSON file with extra VPS test hosts")
    sp.add_argument("--token", default=os.environ.get("GLOBALPING_TOKEN"), help="Globalping API token")
    sp.add_argument("--json", help="write full results to this file")
    sp.set_defaults(func=cmd_scan)

    pp = sub.add_parser("probe", help="measure ICMP and game UDP latency to the target right now")
    pp.add_argument("target")
    pp.add_argument("--udp", type=validate_udp_spec, help="e.g. nq:26000, qw:27500, q3:27960")
    pp.add_argument("--count", type=int, default=25)
    pp.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    if args.target.startswith("-"):
        ap.error("target must be a hostname or IP, not a ping option")
    if args.cmd == "probe" and args.count < 1:
        ap.error("--count must be at least 1")
    if args.cmd == "scan":
        if args.budget < 0:
            ap.error("--budget must be zero or greater")
        if not 1 <= args.probes <= 10:
            ap.error("--probes must be between 1 and 10")
        countries = [c.strip() for c in args.countries.split(",")]
        if any(not re.fullmatch(r"[A-Za-z]{2}", c) for c in countries):
            ap.error("--countries must contain two-letter country codes, e.g. US,CA")
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
