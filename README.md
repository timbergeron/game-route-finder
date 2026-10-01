# Game Route Finder

Find a VPS relay location that might lower your ping to a game server. A small,
standard-library Python tool with ICMP, TCP, and Quake UDP measurements.

## A DIY alternative to WTFast-style routing

If you want to work around a poor ISP route with your own relay, this tool helps
you decide where to put it. The idea is similar to game routing services such as
WTFast: an indirect route can sometimes reach a game server faster than your
ISP's direct route. This is an independent project, with no affiliation with WTFast.

Game Route Finder measures and compares candidate routes. You supply the VPS and
configure the tunnel yourself; it does not install a VPN or automatically route
traffic. There is no guarantee that a relay will improve your connection.

## How it works

```text
Direct:   your machine ------------------------> game server
Relay:    your machine ---> VPS relay ----------> game server

Estimated relay RTT = your machine → VPS RTT + VPS network → server RTT
```

1. Measure your direct route to the target.
2. Measure your connection to public provider test hosts in the selected countries.
3. Ask [Globalping](https://globalping.io/) probes in matching provider networks and
   cities to measure the target. Prioritize hosts whose first leg is below your
   direct ICMP average.
4. Rank locations by your first-leg average plus the median of second-leg averages.

The second leg comes from a matching network/city probe, **not the exact VPS you
will rent**. The displayed range is variation between probe averages; it is not
an end-to-end worst-case latency guarantee.

## Requirements

- Python 3.8 or newer; no pip packages required.
- The system `ping` command on macOS, Linux, or Windows.
- Internet access to provider test hosts and the Globalping API for `scan`.

Targets currently resolve to IPv4. On Windows, use `py` if `python3` is unavailable.

## Quick start

```sh
git clone https://github.com/timbergeron/game-route-finder.git
cd game-route-finder

# Measure direct ICMP and NetQuake UDP round trips.
python3 route_finder.py probe denver.quakeone.com --udp nq:26000 --count 30

# Rank US VPS locations. Run with your VPN/relay disabled.
python3 route_finder.py scan denver.quakeone.com --udp nq:26000

# Include Canada, survey cities, and save the results.
python3 route_finder.py scan denver.quakeone.com \
  --countries US,CA --survey --budget 150 --json results.json
```

Use your own game server's hostname and query port. Supported UDP queries:

| Flag | Game protocol | Usual port |
| --- | --- | --- |
| `--udp nq:26000` | NetQuake / QSS-M | 26000 |
| `--udp qw:27500` | QuakeWorld | 27500 |
| `--udp q3:27960` | Quake 3 / ioquake3 | 27960 |

`probe` prints the average, best, worst, packet loss, and successful sample count.
UDP figures measure the server's query round trip, including its response time.
Servers may block or rate-limit queries; query loss is not necessarily gameplay loss.

## Verify a relay before keeping it

1. Record `probe` results with your relay off.
2. Scan for a candidate with a meaningful estimated improvement. For example,
   a 20 ms first leg plus a 25 ms second leg suggests 45 ms, compared with a
   hypothetical 70 ms direct route.
3. Deploy a VPS in that provider/location and configure a tunnel such as
   WireGuard, routing the game server's IP through the VPS.
4. Run the same `probe` command with the relay on. Compare averages, worst samples,
   and loss over repeated runs, and confirm in the game itself.
5. Keep the relay only if the measured improvement justifies its cost.

Different machines in the same provider/city can have different routes. ICMP,
TCP connects, and game UDP traffic may also behave differently. The catalog is a
starting point: host availability and provider locations can change. A provider's
public cloud endpoint may take a different path from its VPS instances. The game
server will see the relay's public IP when you tunnel through it.

## Scan options

| Option | Purpose |
| --- | --- |
| `--countries US,CA` | Catalog countries to include; defaults to `US` |
| `--probes 3` | Probes per network/city combination, from 1 to 10 |
| `--budget 150` | Maximum requested Globalping probes across scan **and** survey |
| `--survey` | Compare additional cities across any available network |
| `--cities Denver,Chicago` | Custom survey cities, searched in each selected country |
| `--hosts hosts.json` | Append custom provider test hosts |
| `--token TOKEN` | Globalping token; environment variable preferred |
| `--json results.json` | Save direct measurements, ranked results, and survey |

The budget counts requested probes conservatively, even when a request fails or
returns no usable statistics. Parallel requests reserve budget before submission;
the survey uses only what remains. `--budget 0` performs local measurements without
submitting Globalping measurements. Missing results are shown explicitly.

Default survey cities cover the US. Use `--cities` to survey other countries.

Globalping provides an unauthenticated allowance and higher limits with a token.
The tool checks your current allowance; see the [API documentation](https://globalping.io/docs/api.globalping.io)
for current quotas. To avoid putting a token in shell history:

```sh
read -s GLOBALPING_TOKEN
export GLOBALPING_TOKEN
python3 route_finder.py scan your-game-server.example --budget 150
unset GLOBALPING_TOKEN
```

Globalping measurements are public. Only scan targets you are comfortable sharing
with that service. Tokens are used for API authentication and are not written to
result JSON.

## Custom test hosts

Copy [hosts.example.json](hosts.example.json) and fill in your provider's public
host, country, city names, and network ASN:

```json
[
  {
    "provider": "My provider",
    "name": "My provider Denver",
    "host": "test-host.example.com",
    "country": "US",
    "cities": ["Denver"],
    "asns": [12345]
  }
]
```

The example is a placeholder, not a working test host. Add `"tcp": 443` if the host
accepts TCP connections but blocks ICMP. City/ASN fields select Globalping probes;
a public test host can be reachable even when no matching probe exists.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use local UDP sockets and controlled API responses; they do not require a
Globalping token or submit public measurements. GitHub Actions runs the suite on
Linux and Windows with Python 3.8 and 3.13, plus macOS with Python 3.13.

## License

MIT. See [LICENSE](LICENSE).
