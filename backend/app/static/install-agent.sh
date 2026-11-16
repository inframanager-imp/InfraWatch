#!/bin/bash
# InfraWatch agent installer.
# Usage: curl -sSL <server>/install-agent.sh | sudo bash -s -- --server=<url> --token=<token> --name=<vm-name>
set -e

SERVER=""
TOKEN=""
NAME=""
INTERVAL="30"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server=*) SERVER="${1#*=}"; shift ;;
    --token=*) TOKEN="${1#*=}"; shift ;;
    --name=*) NAME="${1#*=}"; shift ;;
    --interval=*) INTERVAL="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

if [[ -z "$SERVER" || -z "$TOKEN" || -z "$NAME" ]]; then
  echo "Usage: install-agent.sh --server=<url> --token=<token> --name=<vm-name>" >&2
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "This script must be run as root (use sudo)." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found — installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3
  else
    echo "Could not detect apt/dnf/yum. Install python3 manually and re-run this script." >&2
    exit 1
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Warning: docker not found on this VM — container monitoring will report nothing until it is installed." >&2
fi

# websocket-client is the one dependency the agent needs, and only for the
# optional live-log-streaming channel — heartbeat/inventory reporting has
# zero dependencies and keeps working even if this fails to install.
# Newer Debian/Ubuntu (PEP 668) refuse a plain pip install into the system
# Python at all ("externally-managed-environment"). --break-system-packages
# overrides that — acceptable here since this installs exactly one small,
# well-known package for the agent's own use, not something that risks the
# OS's own Python tooling in practice. Try the normal path first so older
# systems that don't enforce PEP 668 aren't unnecessarily forcing anything.
if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
  echo "pip not found — installing..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-pip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-pip
  fi
fi

if command -v pip3 >/dev/null 2>&1; then
  pip3 install --quiet websocket-client 2>/dev/null \
    || pip3 install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
elif python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip install --quiet websocket-client 2>/dev/null \
    || python3 -m pip install --quiet --break-system-packages websocket-client \
    || echo "Warning: could not install websocket-client — live log streaming will be disabled (heartbeat/inventory still work)." >&2
else
  echo "Warning: pip not found and could not be installed — live log streaming will be disabled (heartbeat/inventory still work)." >&2
fi

mkdir -p /opt/infrawatch-agent

cat > /opt/infrawatch-agent/agent.py << 'PYEOF'
#!/usr/bin/env python3
"""InfraWatch agent — reports Docker container and systemd service status.
Outbound-only: this process only ever makes requests to the server; the
server never connects into this VM.
"""
import argparse
import calendar
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import websocket as ws_client  # optional — only needed for live log streaming
except ImportError:
    ws_client = None

AGENT_PATH = "/opt/infrawatch-agent/agent.py"
# The install script is this agent's one source of truth — self-update just
# re-reads the exact same heredoc block a fresh install would write, so
# there's never a second copy of the source to drift out of sync.
_SRC_START = "cat > /opt/infrawatch-agent/agent.py << 'PYEOF'\n"
_SRC_END = "\nPYEOF\n"

# Delta state between heartbeats, for CPU %.
_last_cpu = None

# Live-log-streaming state: stream_id -> subprocess.Popen. Guards concurrent
# writes to the single shared websocket, since multiple stream threads can
# be sending at once.
_active_streams = {}
_ws_send_lock = threading.Lock()


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def run_logs(cmd, timeout=5):
    # docker logs writes the container's stdout and stderr as two separate
    # streams; merge them into one so we don't silently drop stderr output
    # (where most apps put their warnings/errors).
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


def parse_percent(s):
    try:
        return float(s.strip().rstrip("%"))
    except Exception:
        return None


def read_cpu_percent():
    # Delta-since-last-heartbeat, not an instantaneous blocking sample —
    # returns None on the very first heartbeat (no prior sample to diff against).
    global _last_cpu
    try:
        with open("/proc/stat") as f:
            values = list(map(int, f.readline().split()[1:]))
        idle = values[3] + values[4]
        total = sum(values)
        if _last_cpu is None:
            _last_cpu = (total, idle)
            return None
        prev_total, prev_idle = _last_cpu
        _last_cpu = (total, idle)
        d_total, d_idle = total - prev_total, idle - prev_idle
        return round((1 - d_idle / d_total) * 100, 1) if d_total > 0 else None
    except Exception:
        return None


def read_mem_percent():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])
        total, available = info.get("MemTotal"), info.get("MemAvailable")
        return round((1 - available / total) * 100, 1) if total else None
    except Exception:
        return None


def read_disk_percent():
    try:
        usage = shutil.disk_usage("/")
        return round(usage.used / usage.total * 100, 1)
    except Exception:
        return None


def iso_utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def normalize_started(value):
    """docker reports nanoseconds; datetime stops at microseconds.

    A container that has never run reports the zero time, which means "never
    started" rather than a date in year 1.
    """
    value = (value or "").strip()
    if not value or value.startswith("0001-01-01"):
        return None
    head, dot, tail = value.partition(".")
    if not dot:
        return value if value.endswith("Z") else value + "Z"
    digits = "".join(ch for ch in tail if ch.isdigit())[:6]
    return head + "." + digits + "Z"


def container_start_times():
    """The instant each container last started, not docker's "Up 2 hours".

    That string is coarse and relative to the moment it was read, so it goes
    stale between heartbeats. State.StartedAt is the real instant and moves
    on every restart, which is what makes it worth showing beside an alert.
    """
    ids = run(["docker", "ps", "-aq"]).split()
    if not ids:
        return {}
    out = run(["docker", "inspect", "--format", "{{.Name}}|{{.State.StartedAt}}"] + ids, timeout=20)
    started = {}
    for line in out.strip().splitlines():
        name, sep, raw = line.partition("|")
        if sep:
            # inspect prefixes the container name with a slash; ps does not.
            started[name.lstrip("/")] = normalize_started(raw)
    return started


def service_start_times(names):
    """When each unit last entered active state, derived without timezones.

    systemd prints ActiveEnterTimestamp in the VM's local zone with an
    abbreviation that cannot be parsed reliably across hosts. The monotonic
    form is just microseconds since boot, so pair it with /proc/uptime and
    do the arithmetic here, where both numbers come from the same clock.
    """
    if not names:
        return {}
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except Exception:
        return {}
    now = time.time()
    out = run(["systemctl", "show", "--property=Id",
               "--property=ActiveEnterTimestampMonotonic"] + list(names), timeout=20)
    started = {}
    # Blocks are blank-line separated; systemd does not promise the order the
    # properties were asked for, so read each block as a whole.
    for block in out.split(chr(10) + chr(10)):
        props = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key.strip()] = value.strip()
        unit = props.get("Id")
        raw = props.get("ActiveEnterTimestampMonotonic")
        if not unit or not raw:
            continue
        try:
            since_boot = int(raw) / 1000000.0
        except ValueError:
            continue
        # Zero means the unit has never been active, not "started at boot".
        if since_boot > 0:
            started[unit] = iso_utc(now - (uptime - since_boot))
    return started


def collect_containers():
    out = run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"])
    containers = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue
        name, image, status = parts[0], parts[1], parts[2]
        ports = parts[3] if len(parts) > 3 else ""
        containers.append({"name": name, "image": image, "status": status, "ports": ports or None})

    # One bulk call for live resource stats (only returns running containers).
    stats_out = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"], timeout=10)
    stats_by_name = {}
    for line in stats_out.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            stats_by_name[parts[0]] = {"cpu": parts[1], "mem": parts[2]}

    for c in containers:
        stat = stats_by_name.get(c["name"], {})
        c["cpu_percent"] = parse_percent(stat.get("cpu", ""))
        c["mem_usage"] = stat.get("mem")
        # Snapshot only — not live streaming. Kept short (--tail) and
        # per-container timeout small so this can't meaningfully delay
        # the heartbeat even with several containers.
        c["logs"] = run_logs(["docker", "logs", "--tail", "50", "--timestamps", c["name"]])

    started = container_start_times()
    for c in containers:
        c["started_at"] = started.get(c["name"])
        rc = run(["docker", "inspect", "-f", "{{.RestartCount}}", c["name"]], timeout=5).strip()
        c["restart_count"] = int(rc) if rc.isdigit() else None
    return containers


# Units written into /etc/systemd/system by a package manager rather than by
# a person. snapd generates one per snap (snap.lxd.daemon.service and friends)
# and they are no more hand-written than anything under /lib -- several are
# oneshots or socket-activated, so "inactive (dead)" is their normal resting
# state and treating them as application units reports healthy machines as
# broken.
GENERATED_UNIT_PREFIXES = ("snap.",)


def is_custom_unit(name):
    # A hand-written unit lives directly at /etc/systemd/system/<name> — this
    # is how every OS package instead ships its units under /lib or
    # /usr/lib/systemd/system, and it's also exactly how this agent's own
    # service and every custom app service we've seen gets installed. Pure
    # filesystem check, no subprocess spawned per service.
    if name.startswith(GENERATED_UNIT_PREFIXES):
        return False
    try:
        return os.path.isfile("/etc/systemd/system/" + name)
    except Exception:
        return False


def collect_services():
    out = run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"])
    services = []
    for line in out.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            name, _load, active, sub = parts[0], parts[1], parts[2], parts[3]
            services.append({"name": name, "status": active, "sub_state": sub, "custom": is_custom_unit(name)})

    started = service_start_times([s["name"] for s in services])
    for s in services:
        s["started_at"] = started.get(s["name"])
    return services


STREAM_BACKLOG = 3000
REATTACH_MAX_DELAY = 15
# Long enough for any deploy, short enough that an abandoned viewer
# does not leave a thread waiting indefinitely.
REATTACH_GIVE_UP = 600


def container_id(name):
    """The container currently answering to this name, or None if none is."""
    return run(["docker", "inspect", "-f", "{{.Id}}", name], timeout=5).strip() or None


def docker_follow_cmd(name, tail):
    return ["docker", "logs", "-f", "--tail", str(tail), name]


def file_follow_cmd(path, tail, container=None):
    """-F rather than -f: reopens the file when it is rotated or recreated,
    which plain -f silently stops following after.

    Through docker exec when the file is inside a container -- the VM's own
    tail cannot see a container's filesystem. This needs tail to exist in
    that image, which rules out distroless and scratch bases."""
    cmd = ["tail", "-F", "-n", str(tail), path]
    return ["docker", "exec", container] + cmd if container else cmd


# A wildcard in a registered path stands for every file that matches it, now
# and in future. That is the only workable answer to a log directory that
# gains a new dated file every day.
GLOB_CHARS = "*?["
# An export concatenates these into one argv, and a folder nobody prunes can
# hold a year of them. The newest are the ones worth having.
MAX_GLOB_FILES = 500
GLOB_POLL_SECONDS = 10


def is_glob(path):
    return any(c in path for c in GLOB_CHARS)


def _shell_safe(pattern):
    """A pattern has to reach a shell to be expanded inside a container.

    Only an admin can register one, and an admin already has docker exec on
    this VM, so this is a guard against a typo turning into a command rather
    than a security boundary. The allowed set is what a log path is made of.
    """
    return all(c.isalnum() or c in "/._-*?[]+:@" for c in pattern)


def _epoch(value):
    """Wire time (UTC ISO with a trailing Z) as a unix timestamp."""
    if not value:
        return None
    try:
        stamp = value.replace("Z", "").split(".")[0]
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def glob_matches(pattern, container=None):
    """Matching files as (mtime, path), oldest first.

    Ordered by modification time rather than by name: a rotation scheme that
    counts sorts app.log.10 before app.log.9 alphabetically, and this order
    is what makes a concatenated export read in the right direction.

    Inside a container the times would cost a stat per file, so ls supplies
    the order and the times come back as None.
    """
    if container:
        if not _shell_safe(pattern):
            return []
        out = run(["docker", "exec", container, "sh", "-c",
                   "ls -1tr -d -- " + pattern + " 2>/dev/null"], timeout=20)
        names = [line.strip() for line in out.splitlines() if line.strip()]
        return [(None, n) for n in names][-MAX_GLOB_FILES:]

    found = []
    for path in glob.glob(pattern):
        try:
            if os.path.isfile(path):
                found.append((os.path.getmtime(path), path))
        except OSError:
            continue  # rotated away between the listing and the stat
    found.sort()
    return found[-MAX_GLOB_FILES:]


def newest_match(pattern, container=None):
    files = glob_matches(pattern, container)
    return files[-1][1] if files else None


def glob_range(pattern, container, since, until):
    """The matching files that could hold a line in the range, oldest first.

    Picked by modification time, which for a file rotation has finished with
    is when it was last written: one that stopped growing before the range
    began cannot contain a line inside it. At the far end the first file
    modified after the range closes is kept, because that is the one still
    being written when it closed.

    This over-includes rather than under-includes. A tail of extra lines is
    a nuisance; a silently missing day is a bug.
    """
    start, end = _epoch(since), _epoch(until)
    picked = []
    for mtime, path in glob_matches(pattern, container):
        if mtime is None:
            picked.append(path)  # in a container: no times to filter on
            continue
        if start is not None and mtime < start:
            continue
        picked.append(path)
        if end is not None and mtime > end:
            break  # this one straddles the end, the rest are past it
    return picked


class _NullProc:
    """Stands in for a process the worker has not started yet, so that
    stop_stream has something to pop and the worker can see it was stopped."""

    def terminate(self):
        pass


def _watch_for_newer(stream_id, proc, stop_watch, pattern, container, following):
    """Ends the tail once a newer matching file exists.

    A poll rather than inotify: the agent ships with nothing but the standard
    library, and a file that appears once a day need not be noticed within
    the second.
    """
    while not stop_watch.wait(GLOB_POLL_SECONDS):
        if _active_streams.get(stream_id) is not proc:
            return
        try:
            newest = newest_match(pattern, container)
        except Exception:
            continue
        if newest and newest != following:
            try:
                proc.terminate()
            except Exception:
                pass
            return


def scan_worker(ws, stream_id, pattern, container):
    """Answers a folder scan: every file matching the pattern, oldest first.

    One reply, always sent even when nothing matched -- the server is
    waiting on it, and silence is indistinguishable from an agent too old
    to know the question.
    """
    try:
        files = [path for _, path in glob_matches(pattern, container)]
    except Exception:
        files = []
    try:
        with _ws_send_lock:
            ws.send(json.dumps({"stream_id": stream_id, "files": files}))
    except Exception:
        pass


def glob_stream_worker(ws, stream_id, pattern, container, backlog):
    """Follows the newest file matching a pattern, and moves when it moves.

    tail -F reopens a path that gets replaced, which covers app.log being
    renamed to app.log.1 under it. It does not cover a directory that gains
    a new dated file every day: yesterday's file is never replaced, it just
    stops growing, and following it forever looks exactly like an app that
    went quiet.
    """
    def send(line):
        try:
            with _ws_send_lock:
                ws.send(json.dumps({"stream_id": stream_id, "line": line}))
            return True
        except Exception:
            return False

    _active_streams[stream_id] = _NullProc()
    current = None
    tail = str(backlog)
    waiting_since = None

    try:
        while True:
            if stream_id not in _active_streams:
                return

            newest = newest_match(pattern, container)
            if not newest:
                # Bounded for the same reason the container reattach is: a
                # browser that goes away without a stop would otherwise leave
                # this thread polling a pattern forever, one per viewer.
                if waiting_since is None:
                    waiting_since = time.time()
                    if not send("[infrawatch] nothing matches " + pattern + " yet -- waiting"):
                        return
                if time.time() - waiting_since > REATTACH_GIVE_UP:
                    send("[infrawatch] still nothing matches -- stopped looking")
                    return
                time.sleep(GLOB_POLL_SECONDS)
                continue
            waiting_since = None

            if current is not None and newest != current:
                # Said out loud: the jump from one file's last line to
                # another's first is otherwise baffling.
                if not send("[infrawatch] rotated -- now following " + newest):
                    return
                tail = "+1"  # a file this new is worth reading from its first line
            current = newest

            proc = None
            stop_watch = threading.Event()
            try:
                proc = subprocess.Popen(file_follow_cmd(current, tail, container),
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                _active_streams[stream_id] = proc
                threading.Thread(target=_watch_for_newer,
                                 args=(stream_id, proc, stop_watch, pattern,
                                       container, current),
                                 daemon=True).start()
                for line in proc.stdout:
                    if stream_id not in _active_streams:
                        return
                    if not send(line.rstrip(chr(10))):
                        return
            except Exception:
                pass  # the next pass re-resolves the pattern and tries again
            finally:
                stop_watch.set()
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

            if stream_id not in _active_streams:
                return
            # Backlog only on the first attach and on a genuinely new file;
            # re-tailing the same one would duplicate what is already on
            # screen.
            tail = "0"
            time.sleep(1)
    finally:
        _active_streams.pop(stream_id, None)


def stream_worker(ws, stream_id, cmd, target=None, rebuild=None):
    """Follows a log source and pushes each line over the shared websocket.

    Containers are followed by name across replacements. `docker logs -f` is
    bound to one container: a deploy replaces it, the process exits cleanly,
    and without this the viewer sits on the old container's last line still
    claiming to be live.

    `target` is the container to follow across replacements, and `rebuild`
    builds the follow command for a given backlog -- docker logs for a
    container, a tail through docker exec for a file inside one. Both are
    absent for sources with no container behind them.
    """
    def send(line):
        """False means the socket is gone, which no amount of retrying fixes."""
        try:
            with _ws_send_lock:
                ws.send(json.dumps({"stream_id": stream_id, "line": line}))
            return True
        except Exception:
            return False

    last_id = container_id(target) if target else None
    attempt = cmd
    delay = 1
    waiting_since = None

    try:
        while True:
            proc = None
            try:
                proc = subprocess.Popen(attempt, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                _active_streams[stream_id] = proc
                for line in proc.stdout:
                    if stream_id not in _active_streams:
                        return
                    if not send(line.rstrip(chr(10))):
                        return
                    delay = 1  # output means healthy; forget any earlier backoff
            except Exception:
                pass  # the backoff below covers a command that would not start
            finally:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

            # Stopped from the browser, or nothing worth reattaching to.
            # Only container-backed sources reattach: journalctl -f already
            # survives a unit restart, and a tail -F on the VM reopens its
            # own file.
            if stream_id not in _active_streams or not target:
                return

            time.sleep(delay)
            delay = min(delay * 2, REATTACH_MAX_DELAY)

            current = container_id(target)
            if not current:
                # Bounded deliberately. A browser that goes away without
                # sending stop_stream would otherwise leave this thread
                # waiting on a container that is never coming back, and
                # those accumulate one per abandoned viewer.
                waiting_since = waiting_since or time.time()
                if time.time() - waiting_since > REATTACH_GIVE_UP:
                    send("[infrawatch] container has not come back \u2014 stopped following")
                    return
                continue

            waiting_since = None
            if current != last_id:
                # A new container: replay its history, which is short, and
                # say so, because the jump in content is otherwise baffling.
                last_id = current
                attempt = rebuild(STREAM_BACKLOG)
                if not send("[infrawatch] container was replaced \u2014 following the new one"):
                    return
                delay = 1
            else:
                # Same container, so its history is already on screen and
                # replaying it would duplicate everything.
                attempt = rebuild(0)
    finally:
        _active_streams.pop(stream_id, None)

def _journal_time(value):
    """Translate the wire format (UTC ISO, trailing Z) into journald's.

    systemd's parser takes neither the "T" separator nor "Z", but it does
    accept a trailing zone name, so the instant survives even on a VM whose
    clock is not set to UTC. docker takes the RFC3339 form unchanged, which
    is why only this side needs translating.
    """
    if not value:
        return value
    return value.replace("T", " ").replace("Z", " UTC")


# A line per message meant a million messages for a million lines, all of
# them queueing behind one lock on the socket that also carries heartbeats
# and every live log viewer on this VM. Batched, the same export is a few
# thousand messages and the socket is free again almost immediately.
EXPORT_BATCH_LINES = 500


def export_worker(ws, stream_id, cmd, max_lines):
    """Runs a bounded log dump and sends it, then marks the end explicitly.

    Unlike stream_worker this finishes: the server is collecting into a file
    download and has to know when to stop waiting. The done marker carries a
    reason so a truncated export can say so rather than appearing complete.
    """
    sent = 0
    truncated = False
    proc = None
    batch = []

    def flush():
        """False means the socket is gone, which no amount of retrying fixes."""
        if not batch:
            return True
        try:
            with _ws_send_lock:
                ws.send(json.dumps({"stream_id": stream_id, "batch": batch}))
        except Exception:
            return False
        del batch[:]
        return True

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            if sent >= max_lines:
                truncated = True
                break
            batch.append(line.rstrip(chr(10)))
            sent += 1
            if len(batch) >= EXPORT_BATCH_LINES and not flush():
                return
    except Exception as e:
        # Queued rather than sent here: whatever is already held back has to
        # reach the file before the failure that ended it.
        batch.append(f"[agent] export failed: {e}")
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        # Anything still held back must land before the end marker, or the
        # file is short and claims to be complete.
        flush()
        try:
            with _ws_send_lock:
                ws.send(json.dumps({"stream_id": stream_id, "done": True,
                                    "lines": sent, "truncated": truncated}))
        except Exception:
            pass


def handle_ws_message(ws, msg):
    try:
        data = json.loads(msg)
    except Exception:
        return
    action = data.get("action")
    stream_id = data.get("stream_id")
    if action == "start_stream":
        # Lines of history replayed when a viewer attaches. Every one becomes a
        # DOM node in the browser, so this trades first-load cost for context.
        backlog = str(STREAM_BACKLOG)
        target_type, target_name = data.get("type"), data.get("name")
        container = (data.get("container") or "").strip() or None
        follow, rebuild = None, None
        if target_type == "container":
            follow = container = target_name
            rebuild = lambda tail: docker_follow_cmd(target_name, tail)
        elif target_type == "service":
            cmd = ["journalctl", "-u", target_name, "-f", "-n", backlog, "--no-pager"]
        elif target_type == "file":
            if is_glob(target_name):
                # A pattern has no one file to tail: its own worker resolves
                # the newest match and moves on when rotation makes another.
                threading.Thread(target=glob_stream_worker,
                                 args=(ws, stream_id, target_name, container, backlog),
                                 daemon=True).start()
                return
            follow = container
            rebuild = lambda tail: file_follow_cmd(target_name, tail, container)
        else:
            return
        if rebuild is not None:
            cmd = rebuild(backlog)
        threading.Thread(target=stream_worker,
                         args=(ws, stream_id, cmd, follow, rebuild),
                         daemon=True).start()
    elif action == "export_logs":
        # since/until are ISO timestamps; journalctl and docker both accept
        # that form directly, so no translation is needed.
        target_type, target_name = data.get("type"), data.get("name")
        since, until = data.get("since"), data.get("until")
        max_lines = int(data.get("max_lines") or 200000)
        if target_type == "container":
            cmd = ["docker", "logs", "--timestamps", target_name]
            if since:
                cmd += ["--since", since]
            if until:
                cmd += ["--until", until]
        elif target_type == "service":
            cmd = ["journalctl", "-u", target_name, "--no-pager"]
            if since:
                cmd += ["--since", _journal_time(since)]
            if until:
                cmd += ["--until", _journal_time(until)]
        elif target_type == "file":
            # One file has no notion of a time range, and anything already
            # rotated away is simply gone, so the whole current file is sent
            # and the server notes the range could not be applied.
            #
            # A pattern is the opposite case: rotation left those files right
            # there, and when each was last written says which of them the
            # range can possibly touch.
            container = (data.get("container") or "").strip() or None
            if is_glob(target_name):
                paths = glob_range(target_name, container, since, until)
                if paths:
                    base = ["docker", "exec", container] if container else []
                    cmd = base + ["cat", "--"] + paths
                else:
                    # An empty file would read as "no logs in that range" with
                    # no way to tell it from a pattern that matches nothing.
                    cmd = ["echo", "[agent] no file matched " + target_name]
            elif container:
                cmd = ["docker", "exec", container, "cat", target_name]
            else:
                cmd = ["cat", target_name]
        else:
            return
        threading.Thread(target=export_worker, args=(ws, stream_id, cmd, max_lines), daemon=True).start()
    elif action == "list_files":
        threading.Thread(target=scan_worker,
                         args=(ws, stream_id, data.get("pattern") or "",
                               (data.get("container") or "").strip() or None),
                         daemon=True).start()
    elif action == "stop_stream":
        proc = _active_streams.pop(stream_id, None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass


def run_ws_agent(server, token, name):
    if ws_client is None:
        print("websocket-client not installed — live log streaming disabled (heartbeat/inventory unaffected)", file=sys.stderr)
        return
    ws_url = (
        server.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        + "/agent/ws?name=" + urllib.parse.quote(name) + "&token=" + urllib.parse.quote(token)
    )
    while True:
        try:
            ws = ws_client.create_connection(ws_url, timeout=30)
            print("live-log channel connected")
            while True:
                try:
                    msg = ws.recv()
                except Exception as e:
                    # The socket timeout applies to recv() as well as the
                    # handshake, and the server only ever speaks to start or
                    # stop a stream -- so an idle channel hit this every 30s
                    # and tore down a perfectly healthy connection, taking
                    # every running stream with it. A read timeout is normal:
                    # ping to prove the link is alive and keep waiting.
                    if isinstance(e, ws_client.WebSocketTimeoutException):
                        with _ws_send_lock:
                            ws.ping()
                        continue
                    raise
                if not msg:
                    break
                handle_ws_message(ws, msg)
        except Exception as e:
            print(f"live-log channel error: {e}", file=sys.stderr)
        time.sleep(10)


def extract_agent_source(install_script_text):
    """Pulls the exact bytes the heredoc in install-agent.sh would write to
    AGENT_PATH out of the full script text — same source, same output,
    whether it comes from a fresh install or this function."""
    start = install_script_text.index(_SRC_START) + len(_SRC_START)
    end = install_script_text.index(_SRC_END, start) + 1  # +1 keeps the trailing newline the heredoc itself writes
    return install_script_text[start:end]


def check_for_update(server):
    """Fetches install-agent.sh, extracts the embedded agent source, and — only
    if it's valid Python and actually different from what's on disk — replaces
    the running copy and restarts. The syntax check means a broken deploy on
    the server can't crash-loop every agent in the fleet; it just gets skipped
    until the server-side content is fixed."""
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/install-agent.sh", timeout=20) as resp:
            script_text = resp.read().decode("utf-8")
        latest = extract_agent_source(script_text)
    except Exception as e:
        print(f"update check failed: {e}", file=sys.stderr)
        return

    try:
        compile(latest, AGENT_PATH, "exec")
    except SyntaxError as e:
        print(f"update check: fetched source doesn't parse, skipping this cycle: {e}", file=sys.stderr)
        return

    try:
        with open(AGENT_PATH) as f:
            current = f.read()
    except Exception:
        current = ""

    if hashlib.sha256(latest.encode()).hexdigest() == hashlib.sha256(current.encode()).hexdigest():
        return

    print("update check: newer agent version available, updating and restarting")
    tmp_path = AGENT_PATH + ".new"
    with open(tmp_path, "w") as f:
        f.write(latest)
    os.replace(tmp_path, AGENT_PATH)  # atomic — never leaves a half-written agent.py
    subprocess.Popen(["systemctl", "restart", "--no-block", "infrawatch-agent"])
    time.sleep(2)
    sys.exit(0)


def send_heartbeat(server, token, name):
    payload = {
        "name": name,
        "token": token,
        "cpu_percent": read_cpu_percent(),
        "mem_percent": read_mem_percent(),
        "disk_percent": read_disk_percent(),
        "containers": collect_containers(),
        "services": collect_services(),
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        server.rstrip("/") + "/agent/heartbeat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        print(f"heartbeat ok: {len(payload['containers'])} containers, {len(payload['services'])} services")
    except urllib.error.URLError as e:
        print(f"heartbeat failed: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    threading.Thread(target=run_ws_agent, args=(args.server, args.token, args.name), daemon=True).start()

    update_check_every = max(1, 600 // args.interval)  # roughly every 10 minutes

    # Check once at startup as well as periodically. Without this a restart
    # reset the counter and pushed the first check a full interval away, so
    # restarting an agent to pick up a new version delayed it instead.
    # Safe against looping: an update rewrites agent.py from the same bytes
    # this check compares, so the next startup finds them equal.
    send_heartbeat(args.server, args.token, args.name)
    check_for_update(args.server)

    tick = 0
    while True:
        time.sleep(args.interval)
        send_heartbeat(args.server, args.token, args.name)
        tick += 1
        if tick % update_check_every == 0:
            check_for_update(args.server)


if __name__ == "__main__":
    main()
PYEOF

chmod +x /opt/infrawatch-agent/agent.py

cat > /etc/systemd/system/infrawatch-agent.service << SERVICEEOF
[Unit]
Description=InfraWatch monitoring agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 /opt/infrawatch-agent/agent.py --server=$SERVER --token=$TOKEN --name=$NAME --interval=$INTERVAL
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable infrawatch-agent
# restart (not "enable --now") so re-running this script on a VM that
# already has the agent installed actually picks up new agent.py code —
# --now only starts a service if it isn't already running, which made
# re-installs a silent no-op for any already-running process.
systemctl restart infrawatch-agent

echo "InfraWatch agent installed and started."
echo "Check status: systemctl status infrawatch-agent"
echo "View logs:    journalctl -u infrawatch-agent -f"
