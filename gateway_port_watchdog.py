#!/usr/bin/env python3
"""gateway_port_watchdog.py — verify Hermes gateway port ownership.

WHY THIS EXISTS
---------------
`hermes update` auto-restarts each gateway by having the OLD gateway spawn a
new DETACHED child (venv-holder pattern). That child reparents to init
(PPID=1) and escapes the systemd cgroup. When you later run
`systemctl restart hermes-<profile>`, systemd only kills its own cgroup — the
orphan survives and keeps holding the port. The freshly-started systemd
process then fails to bind (errno 98: address already in use), parks its
api_server, and the gateway runs DEGRADED while the STALE orphan (old,
unpatched code) keeps serving the port.

Symptom that motivated this: after an update, OWUI tool cards lost their
`arguments`/`result` (a patched-code behavior) even though the patch files
were on disk and `systemctl is-active` said "active".

WHAT THIS CHECKS
----------------
  legitimate holders = MainPID of every RUNNING Hermes gateway service
                       (scans BOTH user- and system-level systemd)
  actual holders     = PIDs listening on the gateway ports (default 30000-30005)

Any actual holder NOT in the legitimate set is an ORPHAN. A service whose
expected port is held by a different PID is a MISMATCH (its api_server is
parked).

Both levels are scanned and unioned, so a deployment that mixes user-level
(meowhome) and system-level (meowplace) services — or has a stale/failed
unit at the other level — is handled without guessing.

REPORT-ONLY BY DEFAULT. Pass --fix to kill orphans (SIGTERM, then SIGKILL)
and restart the service that should own the port.

USAGE
-----
  python3 gateway_port_watchdog.py            # report only
  python3 gateway_port_watchdog.py --fix      # report + auto-remediate
  python3 gateway_port_watchdog.py --ports 30000-30005,30010
  python3 gateway_port_watchdog.py --json     # machine-readable

Exit codes: 0 = all clean, 1 = orphan/mismatch found, 2 = usage/env error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ── helpers ────────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 20) -> str:
    """Run a command, return stdout (empty on failure). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


def systemctl(level: str, *args: str) -> str:
    base = ["systemctl", "--user"] if level == "user" else ["systemctl"]
    return run(base + list(args))


def ss_listeners() -> dict[int, list[int]]:
    """Map listening TCP port -> [pids]. Uses `ss -tlnp`.

    Parses the Local Address:Port column (field index 3) with rsplit so it
    works for IPv4 (0.0.0.0:30000, 127.0.0.1:30005) and IPv6 ([::]:30000).
    """
    out = run(["ss", "-tlnp"])
    port_pids: dict[int, list[int]] = {}
    pid_re = re.compile(r"pid=(\d+)")
    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] != "LISTEN" or len(parts) < 5:
            continue
        local = parts[3]                      # e.g. 0.0.0.0:30000 / [::]:30000
        if ":" not in local:
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        pids = [int(x) for x in pid_re.findall(line)]
        if pids:
            port_pids.setdefault(port, []).extend(pids)
    return port_pids


def pid_start(pids: list[int]) -> dict[int, str]:
    """pid -> human start time (from `ps -o lstart`)."""
    if not pids:
        return {}
    out = run(["ps", "-o", "pid=,lstart=", "-p", ",".join(map(str, pids))])
    res: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            try:
                res[int(parts[0])] = parts[1].strip()
            except ValueError:
                pass
    return res


def pid_ppid(pids: list[int]) -> dict[int, int]:
    if not pids:
        return {}
    out = run(["ps", "-o", "pid=,ppid=", "-p", ",".join(map(str, pids))])
    res: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            try:
                res[int(parts[0])] = int(parts[1])
            except ValueError:
                pass
    return res


# ── model ──────────────────────────────────────────────────────────────────

@dataclass
class Service:
    name: str          # e.g. hermes-gateway-coder.service / hermes-coder.service
    profile: str       # e.g. coder
    level: str         # "user" | "system"
    main_pid: int      # 0 if not running
    port: int | None   # from profile .env API_SERVER_PORT


@dataclass
class Report:
    services: list[Service] = field(default_factory=list)
    orphans: list[dict] = field(default_factory=list)      # port held by non-MainPID
    mismatches: list[dict] = field(default_factory=list)   # service port held by other pid
    clean: bool = True


# ── discovery ──────────────────────────────────────────────────────────────

def _units(level: str) -> str:
    base = ["systemctl", "--user"] if level == "user" else ["systemctl"]
    return run(base + ["list-units", "--type=service", "--no-pager"])


def _parse_units(level: str) -> list[tuple[str, str]]:
    """Return (service_name, profile) pairs for gateway services at `level`.

    Gateway units may be named hermes-<profile>.service (system level, or
    user level e.g. blub) or hermes-gateway-<profile>.service (user level,
    supervised mode). Both forms are matched at either level; the profile is
    the name with the hermes- / hermes-gateway- prefix stripped.
    (hermes-tool-filter is excluded.)
    """
    out = _units(level)
    pairs: list[tuple[str, str]] = []
    for m in re.finditer(r"(hermes-[a-z0-9-]+)\.service", out):
        name = m.group(1)
        if name == "hermes-tool-filter":
            continue
        prefix = "hermes-gateway-" if name.startswith("hermes-gateway-") else "hermes-"
        pairs.append((name, name[len(prefix):]))
    return pairs


def read_profile_port(profile: str) -> int | None:
    env = Path.home() / ".hermes" / "profiles" / profile / ".env"
    try:
        for line in env.read_text().splitlines():
            m = re.match(r"^\s*API_SERVER_PORT\s*=\s*(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def enumerate_services() -> list[Service]:
    """Scan BOTH user and system levels; return all gateway services."""
    services: list[Service] = []
    seen: set[tuple[str, str]] = set()
    for level in ("user", "system"):
        for name, profile in _parse_units(level):
            key = (level, name)
            if key in seen:
                continue
            seen.add(key)
            main_pid = int(systemctl(level, "show", "-p", "MainPID", "--value",
                                     name + ".service").strip() or 0)
            services.append(Service(name=name + ".service", profile=profile,
                                    level=level, main_pid=main_pid,
                                    port=read_profile_port(profile)))
    return services


# ── core check ─────────────────────────────────────────────────────────────

def check(ports: set[int]) -> Report:
    services = enumerate_services()
    rep = Report(services=services)

    listeners = ss_listeners()
    legitimate = {s.main_pid for s in services if s.main_pid > 0}

    # port -> owning service; prefer a RUNNING service if two claim the port
    port_to_service: dict[int, Service] = {}
    for s in services:
        if s.port is None:
            continue
        cur = port_to_service.get(s.port)
        if cur is None or (cur.main_pid == 0 and s.main_pid > 0):
            port_to_service[s.port] = s

    all_pids = set(legitimate)
    for pids in listeners.values():
        all_pids.update(pids)
    starts = pid_start(sorted(all_pids))
    ppid = pid_ppid(sorted(all_pids))

    # 1) orphans: a gateway port held by a PID that is not a running MainPID
    for port in sorted(ports):
        for pid in listeners.get(port, []):
            if pid in legitimate:
                continue
            svc = port_to_service.get(port)
            rep.clean = False
            rep.orphans.append({
                "port": port,
                "pid": pid,
                "started": starts.get(pid, "?"),
                "ppid": ppid.get(pid, -1),
                "expected_service": svc.name if svc else None,
            })

    # 2) mismatches: a running service's expected port is held by a different PID
    for s in services:
        if s.port is None or s.main_pid == 0:
            continue
        holders = listeners.get(s.port, [])
        if holders and s.main_pid not in holders:
            rep.clean = False
            rep.mismatches.append({
                "service": s.name,
                "port": s.port,
                "main_pid": s.main_pid,
                "held_by": holders,
            })

    return rep


# ── remediation ────────────────────────────────────────────────────────────

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        print(f"  ! cannot signal {pid} (permission) — try sudo", file=sys.stderr)
        return False
    for _ in range(10):
        time.sleep(0.5)
        if not _alive(pid):
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(1)
    return not _alive(pid)


def fix(rep: Report) -> int:
    """Kill orphans, then restart the service that should own each freed port."""
    if not rep.orphans:
        print("Nothing to fix (no orphans).")
        return 0
    port_to_service: dict[int, Service] = {}
    for s in rep.services:
        if s.port is None:
            continue
        cur = port_to_service.get(s.port)
        if cur is None or (cur.main_pid == 0 and s.main_pid > 0):
            port_to_service[s.port] = s

    restarted: set[str] = set()
    rc = 0
    for o in rep.orphans:
        print(f"Killing orphan pid={o['pid']} (port {o['port']}, started {o['started']})")
        ok = kill_pid(o["pid"])
        print(f"  -> {'killed' if ok else 'FAILED to kill'}")
        if not ok:
            rc = 1
        svc = port_to_service.get(o["port"])
        if svc and svc.name not in restarted:
            restarted.add(svc.name)
            cmd = ["systemctl", "--user"] if svc.level == "user" else ["systemctl"]
            print(f"Restarting {svc.name}")
            run(cmd + ["restart", svc.name], timeout=60)
    if restarted:
        print("Waiting 5s for rebind...")
        time.sleep(5)
    return rc


# ── output ─────────────────────────────────────────────────────────────────

def print_report(rep: Report) -> None:
    print("Hermes gateway port watchdog")
    print(f"  services discovered: {len(rep.services)}")
    for s in rep.services:
        state = f"MainPID={s.main_pid}" if s.main_pid else "NOT RUNNING"
        port = f"port={s.port}" if s.port else "port=?"
        print(f"    - [{s.level:<6}] {s.name:<40} {state:<16} {port}")
    print()
    if rep.clean:
        print("RESULT: CLEAN — every gateway port is held by its systemd MainPID.")
        return
    if rep.orphans:
        print(f"RESULT: {len(rep.orphans)} ORPHAN holder(s) — port held by a non-systemd PID:")
        for o in rep.orphans:
            print(f"    port {o['port']}: pid={o['pid']} ppid={o['ppid']} started={o['started']}"
                  f"  (expected {o['expected_service']})")
    if rep.mismatches:
        print(f"RESULT: {len(rep.mismatches)} MISMATCH — service api_server likely parked:")
        for m in rep.mismatches:
            print(f"    {m['service']}: port {m['port']} MainPID={m['main_pid']} but held_by={m['held_by']}")
    print("\nFix: kill the orphan pid(s), then restart the expected service (or run with --fix).")


def parse_ports(spec: str) -> set[int]:
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            ports.update(range(int(lo), int(hi) + 1))
        else:
            ports.add(int(part))
    return ports


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Hermes gateway port ownership.")
    ap.add_argument("--fix", action="store_true", help="kill orphans + restart owning service")
    ap.add_argument("--ports", default="30000-30005",
                    help="comma list / ranges of ports to inspect (default 30000-30005)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    ports = parse_ports(args.ports)
    rep = check(ports)

    if args.json:
        print(json.dumps({
            "clean": rep.clean,
            "services": [s.__dict__ for s in rep.services],
            "orphans": rep.orphans,
            "mismatches": rep.mismatches,
        }, indent=2))
    else:
        print_report(rep)

    if args.fix and not rep.clean:
        rc = fix(rep)
        rep2 = check(ports)
        print("\n--- post-fix re-verify ---")
        if args.json:
            print(json.dumps({"clean": rep2.clean, "orphans": rep2.orphans,
                              "mismatches": rep2.mismatches}, indent=2))
        else:
            print_report(rep2)
        return 0 if rep2.clean else rc
    return 0 if rep.clean else 1


if __name__ == "__main__":
    sys.exit(main())
