#!/usr/bin/env bash
# mooncake-loadtest.sh: one-shot Mooncake Store L2 (DRAM) / L3 (NVMe) load test and hardware audit.
#
# Installs everything, measures every ceiling the store sits on, drives the real Mooncake
# store (mooncake_master + a store leg with NVMe offload per RDMA NIC) past its configured DRAM and disk
# capacity, and prints a strict PASS/WARN/FAIL report: per-tier throughput against the box's
# own ceilings, the resource that bottlenecked every phase, and every error counter that moved
# (PCIe AER correctable/uncorrectable, EDAC memory CE/UE, NVMe SMART + error log, RDMA port and
# hw counters, NIC PHY/FEC/pause/discard counters, optics alarms, thermal throttling, dmesg).
#
# ONE HOST (store and load generator on the same box, RDMA loopback through each NIC):
#   curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash
#
# TWO HOSTS (store box = server, engine stand-in = client; NIC i on the client talks to NIC i on the server):
#   server:  curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash -s -- server
#   client:  curl -fsSL https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh | sudo bash -s -- client --server <server-mgmt-ip>
#
# Common flags (any mode):
#   --profile quick|standard|soak   quick ~10-15 min (default); standard ~45 min; soak uses bounded disk budget, hours
#   --protocol auto|rdma|tcp        auto = rdma with an ACTIVE IPv4 port, else tcp diagnostic (reported as FAIL)
#   --nics mlx5_0,mlx5_1            RDMA devices to use, one store lane per device (default: every ACTIVE port with IPv4)
#   --disk-dirs /mnt/a,/mnt/b       L3 directory per lane (default: largest NVMe-backed filesystem, one subdir per lane)
#   --segment-gb N                  L2 DRAM segment per lane (default: 30% of RAM / lanes; quick caps it at 16)
#   --disk-cap-gb N                 L3 capacity per lane (quick 4x segment; all capped to available space minus headroom)
#   --object-bytes N                object size (default 21233664 = one 1536-token MLA KV block, 576 B x 24 layers)
#   --workers N --batch N           load generator processes per lane (16) and keys per batch call (4)
#   --fill-ratio F                  puts F x the L3 capacity (default 1.1: crosses DRAM eviction AND disk eviction)
#   --skip fabric,dram,nvme,fs,store  skip phases
#   --wheel non-cuda|cuda12|cuda13  Mooncake wheel flavour (default non-cuda: a store box needs no GPU stack)
#   --mooncake-version X.Y.Z        pin the wheel (default: latest on PyPI)
#   --lenient                       correctable PCIe/memory/NVMe-log errors are WARN instead of FAIL
#   --no-install                    skip apt/dnf/pip (reuse /opt/mooncake-loadtest)
# Other modes:
#   report <run-dir>                re-render the report of a finished run
#   clean                           kill leftover masters/legs/benches from an interrupted run
#
# Output: /opt/mooncake-loadtest/runs/<timestamp>-<host>/ (report.txt, report.json, raw data, logs) and a .tar.gz of it.
# Exit code: 0 = no FAIL, 1 = at least one FAIL, 2 = the test itself could not run.
#
# Requirements: root, Linux (Ubuntu/Debian or RHEL-family), Python 3.10-3.13 (installed if missing), internet for pip.
# Wire path: a put is an RDMA WRITE from the client's registered buffer into the leg's DRAM segment; a get is an RDMA
# READ out of it; the leg persists objects into 1 GiB bucket files on L3 (buffered pwritev + fdatasync) and restores
# evicted ones with O_DIRECT io_uring reads into a registered staging arena. Every phase below measures one hop of that.
set -euo pipefail

MCL_VERSION=2026.10.13
MCL_URL=https://raw.githubusercontent.com/morphllm/mooncake-loadtest/main/mooncake-loadtest.sh
WORK=${MCL_WORK:-/opt/mooncake-loadtest}
LIB=$WORK/lib
VENV=$WORK/venv
PY=$VENV/bin/python
PORT_BASE=50100              # lane i: master rpc +0, metrics +1, leg http +3, leg api +4, TE target +5, perftest +6/+7, TE initiator +8
CTL_PORT_DEFAULT=50090

if [ -t 2 ]; then C_C=$'\033[36m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'; else C_C=''; C_Y=''; C_R=''; C_0=''; fi
log()  { printf '%s[mcl %s]%s %s\n' "$C_C" "$(date -u +%H:%M:%S)" "$C_0" "$*" >&2; }
warn() { printf '%s[mcl %s] WARN%s %s\n' "$C_Y" "$(date -u +%H:%M:%S)" "$C_0" "$*" >&2; }
die()  { printf '%s[mcl %s] FATAL%s %s\n' "$C_R" "$(date -u +%H:%M:%S)" "$C_0" "$*" >&2; exit 2; }
now()  { date +%s.%N | cut -c1-14; }
mark() { printf '{"phase":"%s","t0":%s,"t1":%s}\n' "$1" "$2" "$3" >>"$4"; }
port() { echo $((PORT_BASE + $1 * 20 + $2)); }

usage() { sed -n '2,46p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
  echo "usage: mooncake-loadtest.sh [single|server|client|report <dir>|clean] [flags]  (see header of the script)"; exit 2; }

# ════════════════════════════════════════════════════════════════════════════════════════════
# Install: system tools + an isolated venv with the Mooncake wheel. Idempotent.
# ════════════════════════════════════════════════════════════════════════════════════════════
APT_PKGS="python3 python3-venv python3-pip fio nvme-cli pciutils dmidecode numactl iproute2 ethtool rdma-core ibverbs-providers
  ibverbs-utils infiniband-diags perftest libibverbs1 librdmacm1 libnuma1 libcurl4 libgomp1 gcc libc6-dev curl tar
  util-linux procps kmod"
DNF_PKGS="python3 python3-pip fio nvme-cli pciutils dmidecode numactl iproute ethtool rdma-core libibverbs libibverbs-utils
  librdmacm infiniband-diags perftest numactl-libs libcurl libgomp gcc glibc-devel curl tar util-linux procps-ng kmod"

install_system() {
  if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    log "apt: installing tools"
    apt-get update -qq || warn "apt-get update failed"
    # shellcheck disable=SC2086
    if ! apt-get install -y --no-install-recommends $APT_PKGS; then
      for p in $APT_PKGS; do apt-get install -y -qq --no-install-recommends "$p" >/dev/null 2>&1 || warn "apt: $p unavailable"; done
    fi
  elif command -v dnf >/dev/null || command -v yum >/dev/null; then
    local pm; pm=$(command -v dnf || command -v yum)
    log "dnf: installing tools"
    # shellcheck disable=SC2086
    if ! "$pm" install -y $DNF_PKGS; then
      for p in $DNF_PKGS; do "$pm" install -y -q "$p" >/dev/null 2>&1 || warn "dnf: $p unavailable"; done
    fi
  else
    warn "no apt-get/dnf: install fio nvme-cli pciutils dmidecode numactl ethtool rdma-core perftest gcc python3 yourself"
  fi
}

pick_python() {   # the Mooncake wheels are cp310-cp313
  local c v
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null || continue
    v=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null) || continue
    [ "$v" -ge 310 ] && [ "$v" -le 313 ] && { echo "$c"; return; }
  done
  if command -v dnf >/dev/null; then dnf install -y -q python3.12 python3.12-pip >/dev/null 2>&1 && { echo python3.12; return; }; fi
  if command -v apt-get >/dev/null; then apt-get install -y -qq python3.12 python3.12-venv >/dev/null 2>&1 && { echo python3.12; return; }; fi
  managed_python
}

managed_python() {
  mkdir -p "$WORK/bin" "$WORK/python"
  if [ ! -x "$WORK/bin/uv" ]; then
    log "no compatible system Python: installing isolated uv runtime"
    curl -fsSL https://astral.sh/uv/install.sh -o "$WORK/uv-install.sh" || return 1
    UV_INSTALL_DIR="$WORK/bin" UV_NO_MODIFY_PATH=1 sh "$WORK/uv-install.sh" >&2 || return 1
    rm -f "$WORK/uv-install.sh"
  fi
  UV_PYTHON_INSTALL_DIR="$WORK/python" "$WORK/bin/uv" python install 3.13 >&2 || return 1
  UV_PYTHON_INSTALL_DIR="$WORK/python" "$WORK/bin/uv" python find --managed-python 3.13
}

install_python() {
  local py wheel
  py=$(pick_python) || die "need Python 3.10-3.13 for the Mooncake wheel"
  case "$WHEEL" in
    non-cuda) wheel=mooncake-transfer-engine-non-cuda ;;
    cuda12)   wheel=mooncake-transfer-engine ;;
    cuda13)   wheel=mooncake-transfer-engine-cuda13 ;;
    *) die "--wheel non-cuda|cuda12|cuda13" ;;
  esac
  [ -x "$PY" ] || "$py" -m venv "$VENV" || die "venv creation failed (install python3-venv)"
  log "pip: $wheel${MC_VERSION:+==$MC_VERSION} + numpy into $VENV"
  "$PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
  for w in mooncake-transfer-engine-non-cuda mooncake-transfer-engine mooncake-transfer-engine-cuda13; do
    [ "$w" = "$wheel" ] || "$PY" -m pip uninstall -q -y "$w" >/dev/null 2>&1 || true   # flavours share the package dir
  done
  "$PY" -m pip install "$wheel${MC_VERSION:+==$MC_VERSION}" numpy || die "pip install $wheel failed"
  if [ "$WHEEL" != non-cuda ] && ! ldconfig -p | grep -q 'libcuda.so.1'; then
    # A CUDA wheel links libcuda/libcudart even when no GPU is used: the driver userspace + pip runtime satisfy it.
    "$PY" -m pip install -q nvidia-cuda-runtime-cu12 >/dev/null 2>&1 || true
    "$PY" -c 'import nvidia.cuda_runtime as m; print(m.__path__[0] + "/lib")' >/etc/ld.so.conf.d/mcl-cuda-runtime.conf 2>/dev/null && ldconfig || true
    if command -v apt-get >/dev/null; then
      local c; c=$(apt-cache search --names-only '^libnvidia-compute-[0-9]+-server$' | awk '{print $1}' | sort -t- -k3,3n | tail -1)
      [ -n "$c" ] && apt-get install -y -qq --no-install-recommends "$c" >/dev/null 2>&1 || true
    fi
  fi
  "$PY" -c 'from mooncake.store import MooncakeDistributedStore' || die "mooncake.store does not import (see error above)"
  MC_DIR=$("$PY" -c 'import mooncake, os; print(os.path.dirname(mooncake.__file__))')
  [ -x "$MC_DIR/mooncake_master" ] || die "$MC_DIR/mooncake_master missing from the wheel"
  log "installed $(wheel_version) at $MC_DIR"
}
wheel_version() { "$PY" - <<'PY' 2>/dev/null || true
import importlib.metadata as m
for p in ("mooncake-transfer-engine-non-cuda", "mooncake-transfer-engine", "mooncake-transfer-engine-cuda13"):
    try: print(f"{p}=={m.version(p)}", end=""); break
    except m.PackageNotFoundError: pass
PY
}

install_all() {
  mkdir -p "$WORK" "$LIB"
  if [ -z "$NO_INSTALL" ]; then install_system; install_python; else
    [ -x "$PY" ] || die "--no-install but $PY missing"
    MC_DIR=$("$PY" -c 'import mooncake, os; print(os.path.dirname(mooncake.__file__))')
  fi
  write_payload
}

# ════════════════════════════════════════════════════════════════════════════════════════════
# Payload: the Python/C pieces, written next to the venv. Each is a self-contained file.
# ════════════════════════════════════════════════════════════════════════════════════════════
write_payload() {

# ─── probe.py: lanes, L3 dirs, inventory, error-counter snapshots, the 1 Hz sampler ───
cat >"$LIB/probe.py" <<'PY'
"""Host probe: everything read from sysfs/procfs/tools, nothing written.

  lanes     --protocol P --nics a,b         -> JSON list of store lanes (one per RDMA device or one TCP lane)
  diskdirs  --lanes F --dirs a,b --root R   -> JSON list, one L3 directory per lane
  inventory OUT                              -> the box: CPUs, NUMA, DIMMs, NICs, NVMe, PCIe, firmware, tuning
  snapshot  OUT                              -> every error counter that can move during a run
  sample    OUT --stop F --interval S        -> JSONL time series: CPU, threads, disks, NIC bytes, memory, PSI, temps
"""
import argparse, glob, json, os, re, socket, subprocess, sys, time

BDF = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def num(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None


def jsonish(cmd):
    try:
        return json.loads(sh(cmd))
    except json.JSONDecodeError:
        return {}


def pci_chain(sysdev):
    return BDF.findall(os.path.realpath(sysdev))


def pcie_link(sysdev):
    spd = lambda k: (rd(f"{sysdev}/{k}_link_speed").split() or ["?"])[0]
    return {"cur": f"{spd('current')} x{rd(sysdev + '/current_link_width', '?')}",
            "max": f"{spd('max')} x{rd(sysdev + '/max_link_width', '?')}",
            "numa": num(rd(sysdev + "/numa_node", "-1")), "bdf": os.path.basename(os.path.realpath(sysdev)),
            "chain": pci_chain(sysdev)}


def ipv4_of(netdev):
    out = sh(f"ip -4 -o addr show dev {netdev}")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def primary_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def netdev_of_ip(ip):
    for line in sh("ip -4 -o addr show").splitlines():
        p = line.split()
        if len(p) > 3 and p[3].split("/")[0] == ip:
            return p[1]
    return ""


def ib_devices():
    root = "/sys/class/infiniband"
    return sorted(os.listdir(root), key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]) if os.path.isdir(root) else []


def ib_rate_gbit(dev, port="1"):
    m = re.match(r"([\d.]+)", rd(f"/sys/class/infiniband/{dev}/ports/{port}/rate"))
    return float(m.group(1)) if m else None


def ib_netdevs(dev):
    d = f"/sys/class/infiniband/{dev}/device/net"
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def nvme_namespaces():
    return sorted((os.path.basename(p) for p in glob.glob("/sys/block/nvme*n*") if re.fullmatch(r"nvme\d+n\d+", os.path.basename(p))),
                  key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)])


def nvme_ctrl(ns):
    return re.sub(r"n\d+$", "", ns)


def nvme_sysdev(ns):
    """PCI function of a namespace's controller (works with native multipath too)."""
    c = f"/sys/class/nvme/{nvme_ctrl(ns)}/device"
    return c if os.path.exists(c) else f"/sys/block/{ns}/device/device"


# ── lanes ──────────────────────────────────────────────────────────────────────────────────
def cmd_lanes(a):
    lanes = []
    want = [x for x in (a.nics or "").split(",") if x]
    if a.protocol in ("rdma", "auto"):
        for dev in (want or ib_devices()):
            ports = sorted(os.listdir(f"/sys/class/infiniband/{dev}/ports")) if os.path.isdir(f"/sys/class/infiniband/{dev}/ports") else []
            for p in ports:
                state = rd(f"/sys/class/infiniband/{dev}/ports/{p}/state")
                nds = ib_netdevs(dev)
                ip = next((ipv4_of(n) for n in nds if ipv4_of(n)), "")
                if "ACTIVE" in state and ip:
                    lanes.append({"dev": dev, "port": p, "ip": ip, "netdev": next((n for n in nds if ipv4_of(n) == ip), nds[0] if nds else ""),
                                  "numa": num(rd(f"/sys/class/infiniband/{dev}/device/numa_node", "-1")),
                                  "rate_gbit": ib_rate_gbit(dev, p), "state": state,
                                  "link_layer": rd(f"/sys/class/infiniband/{dev}/ports/{p}/link_layer"), "protocol": "rdma"})
                break
    if not lanes:
        if a.protocol == "rdma":
            print(json.dumps({"error": "no ACTIVE RDMA port with an IPv4 address (ibv_devinfo / ibdev2netdev / ip addr)"}))
            return
        ip = primary_ip(); nd = netdev_of_ip(ip)
        spd = num(rd(f"/sys/class/net/{nd}/speed", "-1")) if nd else None
        lanes.append({"dev": "", "port": "", "ip": ip, "netdev": nd, "numa": num(rd(f"/sys/class/net/{nd}/device/numa_node", "-1")) if nd else -1,
                      "rate_gbit": spd / 1000 if spd and spd > 0 else None, "state": "tcp", "link_layer": "tcp", "protocol": "tcp"})
    for i, l in enumerate(lanes):
        l["idx"] = i
    print(json.dumps(lanes))


# ── L3 directories ─────────────────────────────────────────────────────────────────────────
def fs_of(path):
    out = sh(f"findmnt -n -o TARGET,SOURCE,FSTYPE -T {path}").split()
    return {"mount": out[0], "source": out[1], "fstype": out[2]} if len(out) >= 3 else {"mount": "/", "source": "?", "fstype": "?"}


def nvme_under(source):
    """NVMe namespaces beneath a block device (through LVM / md / partitions)."""
    if not source.startswith("/dev/"):
        return []
    names = sh(f"lsblk -s -n -r -o NAME {source}").split()
    return sorted({re.sub(r"p\d+$", "", n) for n in names if re.match(r"nvme\d+n\d+", n)})


def avail_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def cmd_diskdirs(a):
    lanes = json.load(open(a.lanes))
    dirs = [d for d in (a.dirs or "").split(",") if d]
    out = []
    if dirs:
        for i, l in enumerate(lanes):
            d = dirs[i % len(dirs)]
            sub = os.path.join(d, f"mcl-{os.getpid()}-{time.time_ns()}/lane{i}")
            out.append({"lane": i, "base": d, "dir": sub, "shared_by": sum(1 for j in range(len(lanes)) if dirs[j % len(dirs)] == d)})
    else:
        cands = []
        for line in sh("findmnt -rn -b -o TARGET,SOURCE,FSTYPE,AVAIL").splitlines():
            p = line.split()
            if len(p) < 4 or p[2] not in ("xfs", "ext4", "btrfs") or p[0].startswith("/boot"):
                continue
            nv = nvme_under(p[1])
            if nv and int(p[3]) > 20 << 30:
                cands.append({"mount": p[0], "avail": int(p[3]), "nvme": nv,
                              "numa": num(rd(f"{nvme_sysdev(nv[0])}/numa_node", "-1"))})
        cands.sort(key=lambda c: -c["avail"])
        picks = []
        for l in lanes:   # NUMA-local NVMe filesystem first, else the largest
            local = [c for c in cands if c["numa"] == l["numa"] and c not in picks]
            picks.append(local[0] if local else (cands[0] if cands else None))
        for i, c in enumerate(picks):
            base = c["mount"] if c else a.root
            out.append({"lane": i, "base": base, "dir": os.path.join(base, f"mcl-{os.getpid()}-{time.time_ns()}/lane{i}"),
                        "shared_by": sum(1 for x in picks if (x["mount"] if x else a.root) == base)})
    for o in out:
        os.makedirs(o["dir"], exist_ok=True)
        f = fs_of(o["dir"])
        o.update(f, nvme=nvme_under(f["source"]), avail=avail_bytes(o["dir"]))
    print(json.dumps(out))


# ── inventory ──────────────────────────────────────────────────────────────────────────────
def lspci_detail(bdf):
    t = sh(f"lspci -vvv -s {bdf}")
    g = lambda r: (re.search(r, t) or [None, None])[1]
    return {"max_payload": num(g(r"DevCtl:.*?MaxPayload (\d+) bytes") or g(r"MaxPayload (\d+) bytes, MaxReadReq")),
            "max_read_req": num(g(r"MaxReadReq (\d+) bytes")),
            "lnksta": (re.search(r"LnkSta:\s*([^\n]*)", t) or [None, ""])[1].strip(),
            "relaxed_ordering": "RlxdOrd+" in (re.search(r"DevCtl:[^\n]*\n[^\n]*", t) or [""])[0]}


def dimms():
    out, cur = [], {}
    for line in sh("dmidecode -t memory").splitlines():
        line = line.strip()
        if line.startswith("Memory Device"):
            cur = {}; out.append(cur)
        for k in ("Size", "Locator", "Bank Locator", "Type", "Speed", "Configured Memory Speed", "Part Number", "Manufacturer", "Rank"):
            if line.startswith(k + ":"):
                cur[k] = line.split(":", 1)[1].strip()
    return [d for d in out if d.get("Size") and "No Module" not in d["Size"] and d["Size"] not in ("Unknown", "0")]


def ethtool_module(nd):
    t = sh(f"ethtool -m {nd}", timeout=15)
    alarms = [l.strip() for l in t.splitlines() if re.search(r"(alarm|warning)\s*:\s*On", l, re.I)]
    temp = re.search(r"Module temperature\s*:\s*([-\d.]+)", t)
    rx = [float(x) for x in re.findall(r"(?:Rcvr signal avg optical power|Receiver signal average optical power)[^:]*:\s*[\d.]+ mW / ([-\d.]+) dBm", t)]
    return {"present": bool(t.strip()), "alarms_on": alarms, "temp_c": float(temp.group(1)) if temp else None, "rx_dbm": rx,
            "identifier": (re.search(r"Identifier\s*:\s*([^\n]*)", t) or [None, ""])[1].strip()}


def cmd_inventory(a):
    inv = {"host": socket.gethostname(), "kernel": os.uname().release, "time": time.time(),
           "cmdline": rd("/proc/cmdline"), "cpu_model": (re.search(r"model name\s*:\s*([^\n]*)", rd("/proc/cpuinfo")) or [None, os.uname().machine])[1],
           "arch": os.uname().machine, "ncpu": os.cpu_count(), "mem_bytes": int(re.search(r"MemTotal:\s*(\d+)", rd("/proc/meminfo")).group(1)) * 1024,
           "numa": {}, "dimms": dimms(), "nics": {}, "nvme": {}, "tuning": {}, "dmesg_pcie_limited": [], "versions": {}}
    for node in sorted(glob.glob("/sys/devices/system/node/node[0-9]*"), key=lambda p: int(p.rsplit("node", 1)[1])):
        n = node.rsplit("node", 1)[1]
        mem = re.search(r"MemTotal:\s*(\d+)", rd(f"{node}/meminfo"))
        inv["numa"][n] = {"cpus": rd(f"{node}/cpulist"), "mem_bytes": int(mem.group(1)) * 1024 if mem else 0}
    sockets = {rd(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/physical_package_id")}
    inv["sockets"] = len(sockets) or 1
    for dev in ib_devices():
        d = f"/sys/class/infiniband/{dev}"
        nds = ib_netdevs(dev)
        lk = pcie_link(d + "/device")
        ports = {}
        for p in sorted(os.listdir(d + "/ports")) if os.path.isdir(d + "/ports") else []:
            gids = []
            for g in sorted(glob.glob(f"{d}/ports/{p}/gids/*"), key=lambda s: int(s.rsplit("/", 1)[1])):
                v = rd(g); i = g.rsplit("/", 1)[1]
                if v and v != "0000:0000:0000:0000:0000:0000:0000:0000":
                    gids.append({"idx": int(i), "gid": v, "type": rd(f"{d}/ports/{p}/gid_attrs/types/{i}"), "ndev": rd(f"{d}/ports/{p}/gid_attrs/ndevs/{i}")})
            ports[p] = {"state": rd(f"{d}/ports/{p}/state"), "phys_state": rd(f"{d}/ports/{p}/phys_state"), "rate": rd(f"{d}/ports/{p}/rate"),
                        "link_layer": rd(f"{d}/ports/{p}/link_layer"), "gids": gids}
        ibv = sh(f"ibv_devinfo -d {dev}")
        inv["nics"][dev] = {
            "fw": rd(d + "/fw_ver"), "hca": rd(d + "/hca_type"), "board": rd(d + "/board_id"), "node_desc": rd(d + "/node_desc"),
            "pcie": lk, "lspci": lspci_detail(lk["bdf"]), "ports": ports, "netdevs": nds,
            "active_mtu": num((re.search(r"active_mtu:\s*(\d+)", ibv) or [None, None])[1]),
            "mtu": {n: num(rd(f"/sys/class/net/{n}/mtu")) for n in nds}, "ipv4": {n: ipv4_of(n) for n in nds},
            "speed_mbit": {n: num(rd(f"/sys/class/net/{n}/speed")) for n in nds},
            "driver": {n: os.path.basename(os.path.realpath(f"/sys/class/net/{n}/device/driver")) for n in nds},
            "ethtool_i": {n: sh(f"ethtool -i {n}") for n in nds}, "fec": {n: sh(f"ethtool --show-fec {n}") for n in nds},
            "pause": {n: sh(f"ethtool -a {n}") for n in nds}, "ring": {n: sh(f"ethtool -g {n}") for n in nds},
            "pfc": {n: sh(f"dcb pfc show dev {n} 2>/dev/null || mlnx_qos -i {n} 2>/dev/null") for n in nds},
            "ecn": {n: {os.path.relpath(f, f"/sys/class/net/{n}/ecn"): rd(f) for f in glob.glob(f"/sys/class/net/{n}/ecn/*/enable/*")} for n in nds},
            "module": {n: ethtool_module(n) for n in nds},
            "cnp_dscp": {n: rd(f"/sys/class/net/{n}/ecn/roce_np/cnp_dscp") for n in nds}}
    for ns in nvme_namespaces():
        c = nvme_ctrl(ns); sd = nvme_sysdev(ns)
        idns = jsonish(f"nvme id-ns -o json /dev/{ns}")
        lbafs = idns.get("lbafs", [])
        lk = pcie_link(sd)
        inv["nvme"][ns] = {"ctrl": c, "model": rd(f"/sys/class/nvme/{c}/model"), "serial": rd(f"/sys/class/nvme/{c}/serial"),
                           "fw": rd(f"/sys/class/nvme/{c}/firmware_rev"), "bytes": num(rd(f"/sys/block/{ns}/size", "0")) * 512,
                           "lba_bytes": (1 << lbafs[idns.get("flbas", 0) & 0xF].get("ds", 9)) if lbafs else num(rd(f"/sys/block/{ns}/queue/logical_block_size")),
                           "lba_formats": [1 << f.get("ds", 9) for f in lbafs], "pcie": lk, "lspci": lspci_detail(lk["bdf"]),
                           "scheduler": rd(f"/sys/block/{ns}/queue/scheduler"), "nr_requests": num(rd(f"/sys/block/{ns}/queue/nr_requests")),
                           "max_sectors_kb": num(rd(f"/sys/block/{ns}/queue/max_sectors_kb")),
                           "mounts": sh(f"lsblk -n -r -o MOUNTPOINTS /dev/{ns}").split(),
                           "id_ctrl": {k: v for k, v in jsonish(f"nvme id-ctrl -o json /dev/{c}").items() if k in ("mn", "fr", "wctemp", "cctemp", "mdts", "sqes", "nn", "tnvmcap")}}
    gov = sorted({rd(p) for p in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")})
    inv["tuning"] = {"governor": gov, "numa_balancing": rd("/proc/sys/kernel/numa_balancing"),
                     "thp": rd("/sys/kernel/mm/transparent_hugepage/enabled"), "memlock": sh("ulimit -l").strip(),
                     "irqbalance": sh("systemctl is-active irqbalance 2>/dev/null").strip(),
                     "dirty_ratio": rd("/proc/sys/vm/dirty_ratio"), "dirty_background_ratio": rd("/proc/sys/vm/dirty_background_ratio"),
                     "dirty_bytes": rd("/proc/sys/vm/dirty_bytes"), "swappiness": rd("/proc/sys/vm/swappiness"),
                     "io_uring_disabled": rd("/proc/sys/kernel/io_uring_disabled", "absent"),
                     "iommu": " ".join(w for w in rd("/proc/cmdline").split() if "iommu" in w),
                     "smt": rd("/sys/devices/system/cpu/smt/control"), "ntp": sh("timedatectl show -p NTPSynchronized --value 2>/dev/null").strip()}
    dm = sh("dmesg 2>/dev/null")
    inv["dmesg_pcie_limited"] = [l for l in dm.splitlines() if re.search(r"limited by|downgraded|PCIe bandwidth", l)][:50]
    inv["versions"] = {"ofed": sh("ofed_info -s 2>/dev/null").strip(), "rdma_core": sh("dpkg-query -W -f='${Version}' rdma-core 2>/dev/null || rpm -q rdma-core 2>/dev/null").strip(),
                       "nvme_cli": sh("nvme version").strip(), "fio": sh("fio --version").strip(), "os": sh(". /etc/os-release; echo $PRETTY_NAME").strip()}
    json.dump(inv, open(a.out, "w"), indent=1)
    print(f"{inv['host']}: {inv['cpu_model']} x{inv['sockets']} sockets, {inv['ncpu']} cpus, {inv['mem_bytes']/2**30:.0f} GiB, "
          f"{len(inv['dimms'])} DIMMs, {len(inv['nics'])} RDMA devices, {len(inv['nvme'])} NVMe namespaces")
    for dev, v in inv["nics"].items():
        p = v["ports"].get("1", {})
        print(f"  {dev}: {v['hca']} fw {v['fw']} pcie {v['pcie']['cur']} (max {v['pcie']['max']}) numa {v['pcie']['numa']} {p.get('link_layer')} "
              f"{p.get('rate')} {p.get('state')} {v['netdevs']} mtu {v['mtu']} active_mtu {v['active_mtu']} {v['ipv4']}")
    for ns, v in inv["nvme"].items():
        print(f"  {ns}: {v['model']} fw {v['fw']} {v['bytes']/1e12:.2f} TB lba {v['lba_bytes']} pcie {v['pcie']['cur']} (max {v['pcie']['max']}) numa {v['pcie']['numa']} {v['mounts']}")


# ── error-counter snapshot ─────────────────────────────────────────────────────────────────
def parse_kv_file(path):
    out = {}
    for line in rd(path).splitlines():
        p = line.split()
        if len(p) == 2 and num(p[1]) is not None:
            out[p[0]] = num(p[1])
    return out


def ethtool_stats(nd):
    out = {}
    for line in sh(f"ethtool -S {nd}").splitlines():
        if ":" in line:
            k, v = line.rsplit(":", 1)
            if num(v.strip()) is not None:
                out[k.strip()] = num(v.strip())
    return out


def cmd_snapshot(a):
    s = {"t": time.time(), "aer": {}, "link": {}, "edac": {}, "rdma": {}, "ethtool": {}, "module": {}, "nvme": {}, "throttle": {}, "dmesg": []}
    for d in glob.glob("/sys/bus/pci/devices/*"):
        rec = {k: parse_kv_file(f"{d}/aer_dev_{k}") for k in ("correctable", "nonfatal", "fatal") if os.path.exists(f"{d}/aer_dev_{k}")}
        for k in ("aer_rootport_total_err_cor", "aer_rootport_total_err_fatal", "aer_rootport_total_err_nonfatal"):
            if os.path.exists(f"{d}/{k}"):
                rec.setdefault("rootport", {})[k] = num(rd(f"{d}/{k}"))
        if rec:
            s["aer"][os.path.basename(d)] = rec
    for dev in ib_devices():
        lk = pcie_link(f"/sys/class/infiniband/{dev}/device")
        s["link"][dev] = lk["cur"]
        for p in os.listdir(f"/sys/class/infiniband/{dev}/ports"):
            base = f"/sys/class/infiniband/{dev}/ports/{p}"
            s["rdma"][f"{dev}/{p}"] = {"counters": {os.path.basename(f): num(rd(f)) for f in glob.glob(base + "/counters/*")},
                                       "hw": {os.path.basename(f): num(rd(f)) for f in glob.glob(base + "/hw_counters/*")},
                                       "state": rd(base + "/state"), "phys_state": rd(base + "/phys_state")}
        for nd in ib_netdevs(dev):
            s["ethtool"][nd] = ethtool_stats(nd)
            s["module"][nd] = ethtool_module(nd)
    if a.netdevs:
        for nd in a.netdevs.split(","):
            if nd and nd not in s["ethtool"]:
                s["ethtool"][nd] = ethtool_stats(nd)
    for ns in nvme_namespaces():
        c = nvme_ctrl(ns)
        if c in s["nvme"]:
            continue
        sm = jsonish(f"nvme smart-log -o json /dev/{c}")
        s["nvme"][c] = {k: v for k, v in sm.items() if isinstance(v, (int, float))}
        s["link"][c] = pcie_link(nvme_sysdev(ns))["cur"]
    for mc in glob.glob("/sys/devices/system/edac/mc/mc*"):
        rec = {k: num(rd(f"{mc}/{k}")) for k in ("ce_count", "ue_count", "ce_noinfo_count", "ue_noinfo_count") if os.path.exists(f"{mc}/{k}")}
        for dimm in glob.glob(f"{mc}/dimm*") + glob.glob(f"{mc}/rank*"):
            lab = rd(dimm + "/dimm_label") or os.path.basename(dimm)
            rec[f"dimm:{lab}"] = {"ce": num(rd(dimm + "/dimm_ce_count")), "ue": num(rd(dimm + "/dimm_ue_count"))}
        s["edac"][os.path.basename(mc)] = rec
    tc = lambda pat: sum(num(rd(p)) or 0 for p in glob.glob(pat))
    s["throttle"] = {"core": tc("/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"),
                     "package": tc("/sys/devices/system/cpu/cpu*/thermal_throttle/package_throttle_count")}
    s["dmesg"] = sh("dmesg 2>/dev/null").splitlines()[-4000:]
    s["mce"] = sh("journalctl -k -q --no-pager -n 2000 2>/dev/null | grep -iE 'mce|machine check|hardware error' | tail -50").splitlines()
    json.dump(s, open(a.out, "w"))


# ── sampler ────────────────────────────────────────────────────────────────────────────────
TAGS = [("mooncake_master", "master"), ("mooncake_store_service", "leg"), ("bench.py", "bench"), ("transfer_engine_bench", "te"),
        ("ib_write_bw", "perftest"), ("ib_read_bw", "perftest"), ("fio", "fio"), ("stream", "stream")]
DISK_RE = re.compile(r"nvme\d+n\d+$|dm-\d+$|md\d+$|sd[a-z]+$|vd[a-z]+$")


def proc_stat(pid):
    t = rd(f"/proc/{pid}/stat")
    if not t:
        return None
    r = t.rfind(")")
    comm = t[t.find("(") + 1:r]
    f = t[r + 2:].split()
    return comm, int(f[1]), int(f[11]) + int(f[12])     # comm, ppid, utime+stime


def cmd_sample(a):
    cmd_cache, tags = {}, {}
    ibs = [(dev, p) for dev in ib_devices() for p in os.listdir(f"/sys/class/infiniband/{dev}/ports")]
    nds = sorted(os.listdir("/sys/class/net"))
    hw = []
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = rd(h + "/name")
        for t in glob.glob(h + "/temp*_input"):
            lab = rd(t.replace("_input", "_label")) or os.path.basename(t)
            dev = os.path.basename(os.path.realpath(h + "/device")) if os.path.exists(h + "/device") else ""
            hw.append((f"{name}:{dev}:{lab}", t))
    with open(a.out, "a") as out:
        k = 0
        while not os.path.exists(a.stop):
            t0 = time.time()
            rec = {"t": round(t0, 3)}
            cpu = {}
            for line in open("/proc/stat"):
                if line.startswith("cpu") and line[3].isdigit():
                    p = line.split(); v = [int(x) for x in p[1:9]]
                    cpu[p[0][3:]] = [sum(v) - v[3] - v[4], sum(v)]
            rec["cpu"] = cpu
            disk = {}
            for line in open("/proc/diskstats"):
                p = line.split()
                if DISK_RE.match(p[2]):
                    disk[p[2]] = [int(p[5]) * 512, int(p[9]) * 512, int(p[12]), int(p[11]), int(p[3]), int(p[7])]
            rec["disk"] = disk   # rd bytes, wr bytes, io_ticks ms, in_flight, rd ios, wr ios
            ib = {}
            for dev, p in ibs:
                b = f"/sys/class/infiniband/{dev}/ports/{p}/counters"
                x, r = num(rd(b + "/port_xmit_data")), num(rd(b + "/port_rcv_data"))
                if x is not None:
                    ib[dev] = [x * 4, r * 4]   # 4-byte words -> bytes
            rec["ib"] = ib
            rec["net"] = {n: [num(rd(f"/sys/class/net/{n}/statistics/tx_bytes")) or 0, num(rd(f"/sys/class/net/{n}/statistics/rx_bytes")) or 0] for n in nds}
            mi = {}
            for line in open("/proc/meminfo"):
                kk, v = line.split(":", 1)
                if kk in ("MemAvailable", "Dirty", "Writeback", "Cached", "MemFree"):
                    mi[kk] = int(v.split()[0]) * 1024
            rec["mem"] = mi
            psi = {}
            for r in ("cpu", "io", "memory"):
                m = re.search(r"some .*total=(\d+)", rd(f"/proc/pressure/{r}"))
                f = re.search(r"full .*total=(\d+)", rd(f"/proc/pressure/{r}"))
                if m:
                    psi[r] = [int(m.group(1)), int(f.group(1)) if f else 0]
            rec["psi"] = psi
            if k % 5 == 0:
                rec["temp"] = {name: (num(rd(p)) or 0) / 1000 for name, p in hw}
            # processes of interest (and their descendants) with per-thread CPU ticks
            procs, parent = {}, {}
            for pd in os.listdir("/proc"):
                if not pd.isdigit():
                    continue
                st = proc_stat(pd)
                if not st:
                    continue
                parent[pd] = st[1]
                if pd not in cmd_cache:
                    cl = rd(f"/proc/{pd}/cmdline").replace("\0", " ")
                    tag = next((tg for pat, tg in TAGS if pat in cl), None)
                    if tag == "master":
                        m = re.search(r"rpc_port=(\d+)", cl); tag += str((int(m.group(1)) - 50100) // 20) if m else ""
                    elif tag == "leg":
                        m = re.search(r"leg-(\d+)\.json", cl); tag += m.group(1) if m else ""
                    cmd_cache[pd] = tag
                procs[pd] = st
            for pd in procs:
                if not cmd_cache.get(pd):
                    q, seen = str(parent.get(pd, 0)), 0
                    while q in procs and seen < 8:
                        if cmd_cache.get(q):
                            tags[pd] = cmd_cache[q]; break
                        q, seen = str(parent.get(q, 0)), seen + 1
                else:
                    tags[pd] = cmd_cache[pd]
            pr = {}
            for pd, tag in list(tags.items()):
                if pd not in procs:
                    tags.pop(pd, None); continue
                th = {}
                for td in glob.glob(f"/proc/{pd}/task/*"):
                    st = proc_stat(f"{pd}/task/{os.path.basename(td)}")
                    if st:
                        th[os.path.basename(td)] = [st[0], st[2]]
                pr[pd] = [tag, procs[pd][0], procs[pd][2], th]
            rec["proc"] = pr
            out.write(json.dumps(rec, separators=(",", ":")) + "\n")
            out.flush()
            k += 1
            time.sleep(max(0.0, a.interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("lanes"); p.add_argument("--protocol", default="auto"); p.add_argument("--nics", default="")
    p = sp.add_parser("diskdirs"); p.add_argument("--lanes", required=True); p.add_argument("--dirs", default=""); p.add_argument("--root", required=True)
    p = sp.add_parser("inventory"); p.add_argument("out")
    p = sp.add_parser("snapshot"); p.add_argument("out"); p.add_argument("--netdevs", default="")
    p = sp.add_parser("sample"); p.add_argument("out"); p.add_argument("--stop", required=True); p.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    {"lanes": cmd_lanes, "diskdirs": cmd_diskdirs, "inventory": cmd_inventory, "snapshot": cmd_snapshot, "sample": cmd_sample}[a.cmd](a)


if __name__ == "__main__":
    main()
PY

# ─── bench.py: the engine's side of the wire, against the real store ───
cat >"$LIB/bench.py" <<'PY'
"""Mooncake Store load generator. One pool of spawned worker processes per lane; each worker
is its own MooncakeDistributedStore client (no segment of its own, like an engine) with one
registered buffer: a read-only random pool that puts are taken from zero-copy (object k is
pool[off(k):off(k)+n]) and a get area that batch_get_into lands in. Content is a pure function
of the key, so any process verifies any object without state.

Phases, every lane at once:
  put        fill_ratio x the lane's L3 capacity: DRAM fills, DRAM eviction, bucket persistence,
             disk-watermark eviction and steady-state churn all happen inside it. Reported as
             overall, burst (until half the segment is written) and churn (the last 10% of bytes).
  exist      batch_is_exist over the oldest 5%: disk eviction must have removed some (full capacity reached).
  get_dram@c newest objects (resident in DRAM), c = 1, W/4, W concurrent workers, time-bounded: L2 read and its scaling.
  get_nvme   objects from the middle of the fill: evicted from DRAM, still on disk -> L3 restores.
  mixed      half put new objects while half read recently successful puts via a bounded shared ring.
A put refused with NO_AVAILABLE_HANDLE (-200: the segment holds only objects pinned until persisted) is
retried with a 20 ms backoff, like the engine connector re-offering a range; refusals and the time spent
waiting are reported separately because they are the DRAM-capacity signature, not a fault.
Workers are spawned, never forked: a forked child of a process holding a live store client deadlocks.
"""
import argparse, itertools, json, mmap, multiprocessing as mp, os, queue, sys, time
import numpy as np

REFUSED = -200          # NO_AVAILABLE_HANDLE
NOT_FOUND = (-704,)     # OBJECT_NOT_FOUND: evicted from every tier = a cache miss
MASK = (1 << 64) - 1
RETRY_S, RETRY_BUDGET_S = 0.02, 600.0


def offset(k, slots):
    return ((((k * 0x9E3779B97F4A7C15) & MASK) >> 17) % slots) * 64


def rolling_keys(ring, start, count):
    """Snapshot recent accepted keys once per batch; empty slots never become gets."""
    ready = []
    with ring.get_lock():
        slots = ring.get_obj()
        for i in range(len(slots)):
            key = slots[(start + i) % len(slots)]
            if key >= 0:
                ready.append(key)
                if len(ready) == count:
                    break
    return ready


def publish_keys(ring, keys, writer, writers, cursor):
    with ring.get_lock():
        slots = ring.get_obj()
        for key in keys:
            slots[(writer + cursor * writers) % len(slots)] = key
            cursor += 1
    return cursor


def worker(cfg, lane, wid, cmd_q, res_q, ring=None):
    from mooncake.store import MooncakeDistributedStore
    node = lane.get("local_numa", -1)
    cpulist = f"/sys/devices/system/node/node{node}/cpulist"
    if node >= 0 and os.path.exists(cpulist):
        cpus = set()
        for span in open(cpulist).read().strip().split(","):
            ends = [int(x) for x in span.split("-")]
            cpus.update(range(ends[0], ends[-1] + 1))
        os.sched_setaffinity(0, cpus & os.sched_getaffinity(0))
    n, B = cfg["object_bytes"], cfg["batch"]
    pool_bytes = (max(64 << 20, 3 * n) + 4095) // 4096 * 4096
    gstride = (n + 4095) // 4096 * 4096
    mm = mmap.mmap(-1, pool_bytes + B * gstride)
    arr = np.frombuffer(mm, dtype=np.uint8)
    arr[:pool_bytes] = np.frombuffer(np.random.default_rng(cfg["seed"]).bytes(pool_bytes), dtype=np.uint8)
    base = arr.ctypes.data
    slots = (pool_bytes - n) // 64
    store = MooncakeDistributedStore()
    rc = store.setup(lane["local_ip"], "P2PHANDSHAKE", 0, cfg["local_buffer"], cfg["protocol"], lane["local_dev"], lane["master"])
    if rc != 0:
        res_q.put(("error", lane["idx"], wid, f"setup rc={rc}")); return
    rc = store.register_buffer(base, pool_bytes + B * gstride)
    if rc != 0:
        res_q.put(("error", lane["idx"], wid, f"register_buffer rc={rc}")); return
    res_q.put(("ready", lane["idx"], wid, os.getpid()))
    prefix = f"{cfg['run_id']}-{lane['idx']}-"
    gbase = pool_bytes
    span = min(4096, n)
    samples = tuple(sorted({0, min(n // 3, n - span), min((2 * n) // 3, n - span), n - span}))

    def keys(c):
        if c["cycle"]:
            lo, hi, step = c["lo"], c["hi"], c["step"]
            k = c["start"] - lo
            while True:
                yield lo + (k % (hi - lo)); k += step
        else:
            yield from range(c["start"], c["hi"], c["step"])

    while True:
        c = cmd_q.get()
        if c is None:
            break
        st = {"bytes": 0, "ops": 0, "objs": 0, "errors": {}, "refusals": 0, "refusal_wait_s": 0.0, "deadline_refused": 0, "misses": 0, "mismatch": 0,
              "exist": 0, "offered_bytes": 0, "t0": time.time(), "marks": []}
        lat = []
        published, read_cursor = 0, wid
        it = keys(c)
        nxt = st["t0"] + 1.0
        while True:
            if c.get("deadline") and time.time() >= c["deadline"]:
                break
            if c.get("rolling"):
                batch = rolling_keys(ring, read_cursor, B)
                read_cursor += B
                if not batch:
                    time.sleep(.001)
                    continue
            else:
                batch = list(itertools.islice(it, B if c["op"] != "exist" else 256))
            if not batch:
                break
            names = [prefix + str(k) for k in batch]
            if c["op"] in ("put", "get"):
                st["offered_bytes"] += n * len(batch)
            ts = time.perf_counter()
            if c["op"] == "put":
                pend = list(range(len(batch)))
                while pend:
                    rcs = store.batch_put_from([names[j] for j in pend], [base + offset(batch[j], slots) for j in pend], [n] * len(pend))
                    if len(rcs) != len(pend):
                        raise RuntimeError("incomplete batch_put_from response")
                    retry, accepted = [], []
                    for j, r in zip(pend, rcs):
                        if r == 0:
                            st["bytes"] += n; st["objs"] += 1; accepted.append(batch[j])
                        elif r == REFUSED and time.perf_counter() - ts < RETRY_BUDGET_S and (not c.get("deadline") or time.time() < c["deadline"]):
                            retry.append(j)
                        elif r == REFUSED and c.get("deadline") and time.time() >= c["deadline"]:
                            st["refusals"] += 1
                            st["deadline_refused"] += 1
                        else:
                            st["errors"][str(r)] = st["errors"].get(str(r), 0) + 1
                    if c.get("publish") and accepted:
                        published = publish_keys(ring, accepted, c["writer"], c["writers"], published)
                    if retry:
                        st["refusals"] += len(retry); time.sleep(RETRY_S); st["refusal_wait_s"] += RETRY_S
                    pend = retry
            elif c["op"] == "get":
                rcs = store.batch_get_into(names, [base + gbase + j * gstride for j in range(len(batch))], [n] * len(batch))
                if len(rcs) != len(batch):
                    raise RuntimeError("incomplete batch_get_into response")
                for j, (k, r) in enumerate(zip(batch, rcs)):
                    if r == n:
                        st["bytes"] += n; st["objs"] += 1
                        g, o = gbase + j * gstride, offset(k, slots)
                        if any(not np.array_equal(arr[g + s:g + s + span], arr[o + s:o + s + span]) for s in samples):
                            st["mismatch"] += 1
                    elif r in NOT_FOUND:
                        st["misses"] += 1
                    else:
                        st["errors"][str(r)] = st["errors"].get(str(r), 0) + 1
            else:   # exist
                rcs = store.batch_is_exist(names)
                if len(rcs) != len(batch):
                    raise RuntimeError("incomplete batch_is_exist response")
                st["exist"] += sum(1 for r in rcs if r == 1)
                st["objs"] += len(batch)
            lat.append(time.perf_counter() - ts)
            st["ops"] += 1
            if time.time() >= nxt:
                st["marks"].append((time.time(), st["bytes"])); nxt += 1.0
        st["t1"] = time.time()
        st["marks"].append((st["t1"], st["bytes"]))
        st["lat"] = np.asarray(lat, dtype=np.float32)
        res_q.put(("result", lane["idx"], wid, st))
    store.close()


class Bench:
    def __init__(self, cfg):
        self.cfg = cfg
        ctx = mp.get_context("spawn")
        self.res_q = ctx.Queue()
        self.cmd = {}
        self.procs = []
        self.rings = {}
        for lane in cfg["lanes"]:
            slots = max(1, int(.1 * lane["segment_bytes"] // cfg["object_bytes"]))
            slots = min(slots, 4096)
            self.rings[lane["idx"]] = ctx.Array("q", [-1] * slots)
            for w in range(cfg["workers"]):
                q = ctx.Queue()
                p = ctx.Process(target=worker, args=(cfg, lane, w, q, self.res_q, self.rings[lane["idx"]]), daemon=True)
                p.start(); self.procs.append(p); self.cmd[(lane["idx"], w)] = q
        need, t_end = len(self.cmd), time.time() + 300
        while need:
            try:
                kind, li, w, x = self.res_q.get(timeout=max(1, t_end - time.time()))
            except queue.Empty:
                sys.exit(f"bench: {need} workers not ready after 300 s (store client setup hangs: master/leg reachable on the data IPs?)")
            if kind == "error":
                sys.exit(f"bench: lane {li} worker {w}: {x}")
            need -= 1
        print(f"bench: {len(self.cmd)} store clients ready ({cfg['workers']} per lane x {len(cfg['lanes'])} lanes)", flush=True)
        self.marks = []

    def collect(self, plan):
        end = time.monotonic() + self.cfg.get("phase_timeout_s", 900)
        for _ in plan:
            while True:
                try:
                    result = self.res_q.get(timeout=min(1, max(0.01, end - time.monotonic())))
                    break
                except queue.Empty:
                    if time.monotonic() >= end or any(p.exitcode is not None for p in self.procs):
                        raise RuntimeError("benchmark worker died or exceeded phase timeout")
            yield result

    def run(self, name, plan):
        t0 = time.time()
        for (li, w), c in plan.items():
            self.cmd[(li, w)].put(c)
        outs = {}
        for kind, li, w, st in self.collect(plan):
            if kind == "error":
                sys.exit(f"bench: {name}: lane {li} worker {w}: {st}")
            outs.setdefault(li, []).append(st)
        t1 = time.time()
        self.marks.append({"phase": f"store:{name}", "t0": t0, "t1": t1})
        recs = {}
        for li, sts in outs.items():
            recs[li] = merge(sts, self.cfg["object_bytes"], len(sts))
            r = recs[li]
            print(f"  lane {li} {name:<14} {r['gbps']:7.2f} GB/s  {r['objs']:>8} obj  {r['seconds']:6.1f}s  p50 {r['lat_ms']['p50']:7.2f} ms  "
                  f"p99 {r['lat_ms']['p99']:7.2f} ms  refusals {r['refusals']}  misses {r['misses']}  errors {r['errors'] or 0}  mismatch {r['mismatch']}", flush=True)
        partial = {"phase": name, "results": recs}
        with open(self.cfg["out"] + ".partial.jsonl", "a") as f:
            f.write(json.dumps(partial) + "\n")
        return recs

    def close(self):
        for q in self.cmd.values():
            q.put(None)
        for p in self.procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate(); p.join(timeout=5)


def merge(sts, n, workers, window=None):
    t0, t1 = window or (min(s["t0"] for s in sts), max(s["t1"] for s in sts))
    tot = sum(s["bytes"] for s in sts)
    lat = np.concatenate([s["lat"] for s in sts]) if any(len(s["lat"]) for s in sts) else np.zeros(1, np.float32)
    errors = {}
    for s in sts:
        for k, v in s["errors"].items():
            errors[k] = errors.get(k, 0) + v
    pct = lambda q: float(np.percentile(lat, q) * 1e3)
    r = {"bytes": tot, "objs": sum(s["objs"] for s in sts), "ops": sum(s["ops"] for s in sts), "seconds": t1 - t0,
         "gbps": tot / max(t1 - t0, 1e-9) / 1e9, "workers": workers,
         "offered_gbps": sum(s.get("offered_bytes", s["bytes"]) for s in sts) / max(t1 - t0, 1e-9) / 1e9,
         "lat_ms": {"p50": pct(50), "p90": pct(90), "p99": pct(99), "p999": pct(99.9), "max": float(lat.max() * 1e3), "mean": float(lat.mean() * 1e3)},
         "errors": errors, "refusals": sum(s["refusals"] for s in sts), "refusal_wait_s": sum(s["refusal_wait_s"] for s in sts), "deadline_refused": sum(s.get("deadline_refused", 0) for s in sts),
         "misses": sum(s["misses"] for s in sts), "mismatch": sum(s["mismatch"] for s in sts), "exist": sum(s["exist"] for s in sts),
         "window": [t0, t1]}
    # aggregate progress curve: sum over workers of bytes at each second
    ev = sorted((t, b, i) for i, s in enumerate(sts) for t, b in s["marks"])
    last, cum, curve = [0] * len(sts), 0, []
    for t, b, i in ev:
        cum += b - last[i]; last[i] = b; curve.append((t, cum))
    step = max(1, len(curve) // 4000)
    r["curve"] = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
    return r


def bytes_at(curve, t):
    b = 0
    for tt, bb in curve:
        if tt > t:
            break
        b = bb
    return b


def time_at(curve, target):
    for t, b in curve:
        if b >= target:
            return t
    return curve[-1][0] if curve else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    n, W = cfg["object_bytes"], cfg["workers"]
    if n < 64 or W < 1 or cfg["batch"] < 1 or cfg["sweep_s"] <= 0:
        raise ValueError("object >=64 bytes, positive workers/batch/sweep duration required")
    lanes = cfg["lanes"]
    plan_n = {}
    for l in lanes:
        total = max(int(cfg["fill_ratio"] * l["disk_cap_bytes"] // n), W)
        dram = max(int(0.4 * l["segment_bytes"] // n), 1)
        nv_lo = int(total * 0.45)
        nv = max(min(int(cfg["nvme_get_bytes"] // n), int(total * 0.30)), 1)
        plan_n[l["idx"]] = {"total": total, "dram": dram, "nvme_lo": nv_lo, "nvme_n": nv}
        print(f"lane {l['idx']}: {total} objects x {n} B = {total * n / 1e9:.1f} GB into {l['master']} "
              f"(segment {l['segment_bytes'] / 2**30:.0f} GiB, L3 cap {l['disk_cap_bytes'] / 2**30:.0f} GiB)", flush=True)
        if l["disk_cap_bytes"] <= l["segment_bytes"]:
            print(f"lane {l['idx']}: WARNING L3 cap <= DRAM segment: nothing is forced out to NVMe-only", flush=True)
    b = Bench(cfg)
    try:
        res = {"config": cfg, "plan": plan_n, "phases": {}}
        P = res["phases"]

        def put_plan(lo_of, hi_of, workers, deadline=0.0):
            return {(l["idx"], w): {"op": "put", "lo": lo_of(l), "hi": hi_of(l), "start": lo_of(l) + w, "step": len(workers), "cycle": False,
                                    "deadline": deadline} for l in lanes for w in workers}

        def get_plan(lo_of, hi_of, workers, cycle, deadline=0.0, op="get", step_of=None):
            return {(l["idx"], w): {"op": op, "lo": lo_of(l), "hi": hi_of(l), "start": lo_of(l) + w * (step_of(l) if step_of else 1),
                                    "step": len(workers) * (step_of(l) if step_of else 1), "cycle": cycle, "deadline": deadline}
                    for l in lanes for w in workers}

        allw = list(range(W))
        print("phase put: fill through DRAM into L3, past capacity", flush=True)
        P["put"] = b.run("put", put_plan(lambda l: 0, lambda l: plan_n[l["idx"]]["total"], allw))
        for l in lanes:
            r = P["put"][l["idx"]]
            seg, tot = l["segment_bytes"], r["bytes"]
            tb = time_at(r["curve"], min(seg * 0.5, tot)); r["burst_gbps"] = min(seg * 0.5, tot) / max(tb - r["window"][0], 1e-9) / 1e9
            # churn = the fill after it crossed the L3 capacity (every put now forces persistence + disk eviction),
            # never shorter than the last 10% of the phase so a bunched-up progress curve cannot shrink the window to nothing
            t1 = r["window"][1]
            tc = time_at(r["curve"], l["disk_cap_bytes"]) if tot > l["disk_cap_bytes"] else time_at(r["curve"], tot * 0.9)
            tc = min(tc, t1 - 0.1 * r["seconds"])
            r["churn_gbps"] = (tot - bytes_at(r["curve"], tc)) / max(t1 - tc, 1e-9) / 1e9
            r["churn_window"] = [tc, t1]
            r["refusal_ratio"] = r["refusals"] / max(r["objs"], 1)
            r["refusal_wait_frac"] = r["refusal_wait_s"] / max(r["seconds"] * W, 1e-9)
            print(f"  lane {l['idx']} put burst {r['burst_gbps']:.2f} GB/s  churn {r['churn_gbps']:.2f} GB/s  refusal ratio {r['refusal_ratio']:.2f}  "
                  f"wait frac {r['refusal_wait_frac']:.2f}", flush=True)
            b.marks.append({"phase": f"store:put_churn:{l['idx']}", "t0": tc, "t1": r["window"][1]})

        print("phase exist: is the oldest 5% gone (disk eviction reached)?", flush=True)
        step = lambda l: max(plan_n[l["idx"]]["total"] // 2000, 1)
        P["exist"] = b.run("exist", get_plan(lambda l: 0, lambda l: max(plan_n[l["idx"]]["total"] // 20, 1), [0], False, op="exist", step_of=step))
        for l in lanes:
            r = P["exist"][l["idx"]]; r["head_evicted_frac"] = 1 - r["exist"] / max(r["objs"], 1)

        levels = sorted({1, max(1, W // 4), W})
        for c in levels:
            print(f"phase get_dram@{c}: newest objects, {c} workers/lane, {cfg['sweep_s']} s", flush=True)
            P[f"get_dram@{c}"] = b.run(f"get_dram@{c}", get_plan(lambda l: plan_n[l["idx"]]["total"] - plan_n[l["idx"]]["dram"],
                                                                lambda l: plan_n[l["idx"]]["total"], list(range(c)), True, time.time() + cfg["sweep_s"]))
        print("phase get_nvme: DRAM-evicted objects restored from L3", flush=True)
        P["get_nvme"] = b.run("get_nvme", get_plan(lambda l: plan_n[l["idx"]]["nvme_lo"], lambda l: plan_n[l["idx"]]["nvme_lo"] + plan_n[l["idx"]]["nvme_n"],
                                                   allw, False, time.time() + cfg["nvme_get_max_s"]))
        if cfg["mixed_s"] > 0 and W >= 2:
            print(f"phase mixed: {W // 2} putting + {W - W // 2} reading per lane, {cfg['mixed_s']} s", flush=True)
            dl = time.time() + cfg["mixed_s"]
            pw, gw = list(range(W // 2)), list(range(W // 2, W))
            plan = {}
            for l in lanes:
                t = plan_n[l["idx"]]["total"]
                for i, w in enumerate(pw):
                    plan[(l["idx"], w)] = {"op": "put", "lo": t, "hi": t * 10 + 10**9, "start": t + i, "step": len(pw), "cycle": False, "deadline": dl, "publish": True, "writer": i, "writers": len(pw)}
                for i, w in enumerate(gw):
                    lo = t - plan_n[l["idx"]]["dram"]
                    plan[(l["idx"], w)] = {"op": "get", "lo": lo, "hi": t, "start": lo + i, "step": len(gw), "cycle": True, "deadline": dl, "rolling": True}
            t0 = time.time()
            for (li, w), c in plan.items():
                b.cmd[(li, w)].put(c)
            outs = {}
            for kind, li, w, st in b.collect(plan):
                if kind != "result":
                    raise RuntimeError(f"mixed worker failed: {st}")
                outs.setdefault(li, {}).setdefault("put" if w < W // 2 else "get", []).append(st)
            b.marks.append({"phase": "store:mixed", "t0": t0, "t1": time.time()})
            P["mixed"] = {}
            for li, d in outs.items():
                window = (min(s["t0"] for v in d.values() for s in v), max(s["t1"] for v in d.values() for s in v))
                P["mixed"][li] = {"put": merge(d["put"], n, len(d["put"]), window), "get": merge(d["get"], n, len(d["get"]), window)}
                m = P["mixed"][li]
                print(f"  lane {li} mixed           put {m['put']['gbps']:6.2f} GB/s + get {m['get']['gbps']:6.2f} GB/s  get p99 {m['get']['lat_ms']['p99']:.1f} ms  "
                      f"misses {m['get']['misses']} errors {m['put']['errors'] or 0}/{m['get']['errors'] or 0} mismatch {m['get']['mismatch']}", flush=True)
        for c in levels:
            # Disjoint ranges at each concurrency; successful unique inserts, not overwrites.
            P[f"put_sustained@{c}"] = b.run(f"put_sustained@{c}", put_plan(
                lambda l: plan_n[l["idx"]]["total"] * 100 + c * 10**12,
                lambda l: plan_n[l["idx"]]["total"] * 100 + (c + 1) * 10**12,
                list(range(c)), time.time() + cfg["sweep_s"]))
    finally:
        b.close()
    res["marks"] = b.marks
    json.dump(res, open(cfg["out"], "w"), default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    with open(cfg["marks"], "a") as f:
        for m in b.marks:
            f.write(json.dumps(m) + "\n")


if __name__ == "__main__":
    main()
PY

# ─── ctl.py: the server's control endpoint + the teardown shared by every mode ───
cat >"$LIB/ctl.py" <<'PY'
"""Server control. `serve DIR PORT`: GET /info (lanes, ports, capacities), GET /time (clock-offset probe),
POST /finish (teardown + after-snapshot, responds with DIR as a tar.gz, then exits).
`finish DIR`: the same teardown, used directly in single-host mode."""
import http.server, io, json, os, signal, subprocess, sys, tarfile, threading, time, urllib.request

LIB = os.path.dirname(os.path.abspath(__file__))


def finish(d):
    info = json.load(open(os.path.join(d, "info.json"))) if os.path.exists(os.path.join(d, "info.json")) else {"lanes": []}
    for l in info.get("lanes", []):
        try:
            m = b""
            for host in (l["ip"], "127.0.0.1"):
                try:
                    m = urllib.request.urlopen(f"http://{host}:{l['metrics_port']}/metrics", timeout=5).read(); break
                except OSError:
                    continue
            open(os.path.join(d, "store", f"master-{l['idx']}.metrics"), "wb").write(m)
        except Exception as e:
            open(os.path.join(d, "store", f"master-{l['idx']}.metrics"), "w").write(f"# unavailable: {e}\n")
    pids = os.path.join(d, "pids")
    alive = {}
    if os.path.exists(pids):
        for line in open(pids):
            p = line.split()
            if not p:
                continue
            try:
                os.kill(int(p[0]), 0); alive[p[1] if len(p) > 1 else p[0]] = True
            except OSError:
                alive[p[1] if len(p) > 1 else p[0]] = False
        json.dump(alive, open(os.path.join(d, "alive_at_finish.json"), "w"))
        procs = [l.split() for l in open(pids) if len(l.split()) == 2]
        for first in (True, False):   # legs first: they ping their master until they exit
            batch = [int(p) for p, name in procs if name.startswith("leg") == first]
            for p in batch:
                try:
                    os.kill(p, signal.SIGTERM)
                except OSError:
                    pass
            t_end = time.time() + 15
            while time.time() < t_end and any(_alive(p) for p in batch):
                time.sleep(0.5)
    time.sleep(1)
    for p, _ in procs if os.path.exists(pids) else []:
        if _alive(int(p)):
            try: os.kill(int(p), signal.SIGKILL)
            except OSError: pass
    subprocess.run([sys.executable, os.path.join(LIB, "probe.py"), "snapshot", os.path.join(d, "after.json"),
                    "--netdevs", ",".join(l.get("netdev", "") for l in info.get("lanes", []))])
    open(os.path.join(d, "sampler.stop"), "w").close()
    time.sleep(2.5)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return open(f"/proc/{pid}/stat").read().split(") ")[1][0] != "Z"
    except OSError:
        return False


class H(http.server.BaseHTTPRequestHandler):
    d = None
    def log_message(self, *a):
        pass
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/info":
            self._send(200, open(os.path.join(self.d, "info.json"), "rb").read())
        elif self.path == "/time":
            self._send(200, json.dumps({"t": time.time()}).encode())
        else:
            self._send(404, b"{}")
    def do_POST(self):
        if self.path != "/finish":
            return self._send(404, b"{}")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        open(os.path.join(self.d, "client_marks.json"), "wb").write(body)
        finish(self.d)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(self.d, arcname="server")
        self._send(200, buf.getvalue(), "application/gzip")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    if sys.argv[1] == "finish":
        return finish(sys.argv[2])
    H.d = sys.argv[2]
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", int(sys.argv[3])), H)
    print(f"control endpoint on :{sys.argv[3]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
PY

# ─── report.py: the verdict ───
cat >"$LIB/report.py" <<'PY'
"""The verdict. Reads a run directory, prints and writes report.txt / report.json, exits 1 on any FAIL.

Every bar is relative to a ceiling the run measured on this box (NIC line rate, perftest, raw and filesystem
fio, STREAM), so the report holds on any hardware and every store number says what bounded it. Error
counters are diffed after-before; a counter that moved is listed by name with its delta.
"""
import glob, json, os, re, statistics, sys
from pathlib import Path

STRICT = os.environ.get("MCL_LENIENT", "") == ""
TH = {
    "te_min_frac_line": 0.80, "te_warn_frac_line": 0.90,           # Transfer Engine vs port line rate
    "perftest_min_frac_line": 0.90,                                # raw verbs vs line rate
    "all_lanes_min_frac_sum": 0.90,                                # all NICs at once vs the sum of each alone
    "dram_local_x_nic": 2.0, "dram_remote_x_nic": 1.0,             # STREAM triad vs line rate of the NICs on that node
    "drive_outlier_frac": 0.85,                                    # a drive below 85% of the median of its peers
    "store_put_burst_frac_te": 0.50, "store_put_burst_warn_frac_te": 0.75,
    "store_get_dram_frac_te": 0.50, "store_get_dram_warn_frac_te": 0.75,
    "store_get_nvme_frac_ceiling": 0.50, "store_get_nvme_warn_frac_ceiling": 0.70,
    "store_churn_frac_fs_write": 0.50,
    "l3_read_attribution_min": 0.80,                               # disk bytes read / bytes served in get_nvme
    "nvme_miss_frac_max": 0.01, "tail_p999_over_p50_warn": 20.0,
    "fec_ber_warn": 1e-7, "fec_ber_fail": 1e-5,
    "nvme_temp_warn_c": 70, "nic_module_temp_warn_c": 70, "percent_used_warn": 80,
    "bottleneck_saturated": 0.85, "bottleneck_bound": 0.60,
}
COL = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "INFO": "\033[36m", "SKIP": "\033[90m"} if sys.stdout.isatty() else {}
rows, bottlenecks, notes = [], [], []


def add(section, check, measured, bar, verdict, detail=""):
    rows.append({"section": section, "check": check, "measured": measured, "bar": bar, "verdict": verdict, "detail": detail})


def fmt(x, f="{:.2f}"):
    return "n/a" if x is None else (f.format(x) if isinstance(x, (int, float)) else str(x))


def floor(section, check, measured, bar, warn_bar=None, f="{:.2f}", detail=""):
    if measured is None:
        return add(section, check, "n/a", f">= {fmt(bar, f)}", "SKIP", detail)
    v = "FAIL" if measured < bar else ("WARN" if warn_bar is not None and measured < warn_bar else "PASS")
    add(section, check, fmt(measured, f), f">= {fmt(bar, f)}" + (f" (warn < {fmt(warn_bar, f)})" if warn_bar else ""), v, detail)


def strict(v):
    return "FAIL" if STRICT else "WARN"


def load(p, default=None):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return default


def jsonl(p):
    out = []
    if os.path.exists(p):
        for line in open(p):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ── time series helpers ─────────────────────────────────────────────────────────────────────
class Series:
    def __init__(self, samples, offset=0.0):
        self.s = samples
        for x in self.s:
            x["t"] += offset

    def window(self, t0, t1):
        w = [x for x in self.s if t0 - 0.6 <= x["t"] <= t1 + 0.6]
        return (w[0], w[-1], w) if len(w) >= 2 else (None, None, w)

    def stats(self, t0, t1, inv, lanes):
        a, b, w = self.window(t0, t1)
        if not a:
            return None
        dt = b["t"] - a["t"]
        out = {"dt": dt, "nic": {}, "disk": {}, "cpu_node": {}, "threads": [], "procs": {}, "mem": {}, "psi": {}, "temp_max": {}}
        for dev, (tx, rx) in b.get("ib", {}).items():
            if dev in a.get("ib", {}):
                out["nic"][dev] = {"tx": (tx - a["ib"][dev][0]) / dt / 1e9, "rx": (rx - a["ib"][dev][1]) / dt / 1e9}
        for l in lanes:
            nd = l.get("netdev")
            if l.get("protocol") == "tcp" and nd and nd in b.get("net", {}) and nd in a.get("net", {}):
                out["nic"][nd] = {"tx": (b["net"][nd][0] - a["net"][nd][0]) / dt / 1e9, "rx": (b["net"][nd][1] - a["net"][nd][1]) / dt / 1e9}
        for d, v in b.get("disk", {}).items():
            if d in a.get("disk", {}):
                u = a["disk"][d]
                rd_, wr = (v[0] - u[0]) / dt / 1e9, (v[1] - u[1]) / dt / 1e9
                inflight = statistics.mean(x["disk"].get(d, [0] * 6)[3] for x in w)
                out["disk"][d] = {"read": rd_, "write": wr, "util": min(1.0, (v[2] - u[2]) / 1000 / dt), "inflight": inflight}
        node_cpus = {n: cpulist(v["cpus"]) for n, v in inv.get("numa", {}).items()} if inv else {}
        busy_all = []
        for n, cpus in node_cpus.items():
            bs, ts = 0, 0
            for c in cpus:
                c = str(c)
                if c in b["cpu"] and c in a["cpu"]:
                    bs += b["cpu"][c][0] - a["cpu"][c][0]; ts += b["cpu"][c][1] - a["cpu"][c][1]
            out["cpu_node"][n] = bs / ts if ts else None
        bs = sum(b["cpu"][c][0] - a["cpu"][c][0] for c in b["cpu"] if c in a["cpu"])
        ts = sum(b["cpu"][c][1] - a["cpu"][c][1] for c in b["cpu"] if c in a["cpu"])
        out["cpu_host"] = bs / ts if ts else None
        hz = 100.0
        for pid, (tag, comm, ticks, th) in b.get("proc", {}).items():
            if pid not in a.get("proc", {}):
                continue
            pa = a["proc"][pid]
            out["procs"].setdefault(tag, []).append((ticks - pa[2]) / hz / dt)
            for tid, (tcomm, tt) in th.items():
                if tid in pa[3]:
                    out["threads"].append({"tag": tag, "comm": tcomm, "cpu": (tt - pa[3][tid][1]) / hz / dt})
        out["threads"].sort(key=lambda x: -x["cpu"])
        out["threads"] = out["threads"][:8]
        out["mem"] = {"dirty_wb_max_gb": max((x["mem"].get("Dirty", 0) + x["mem"].get("Writeback", 0)) for x in w) / 1e9,
                      "avail_min_gb": min(x["mem"].get("MemAvailable", 0) for x in w) / 1e9}
        for r, (sm, fu) in b.get("psi", {}).items():
            if r in a.get("psi", {}):
                out["psi"][r] = {"some": (sm - a["psi"][r][0]) / 1e6 / dt, "full": (fu - a["psi"][r][1]) / 1e6 / dt}
        for x in w:
            for k, v in x.get("temp", {}).items():
                out["temp_max"][k] = max(out["temp_max"].get(k, -1e9), v)
        return out


def cpulist(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-"); out += range(int(a), int(b) + 1)
        elif part.strip():
            out.append(int(part))
    return out


# ── load the run ────────────────────────────────────────────────────────────────────────────
def main(run):
    run = Path(run)
    meta = load(run / "meta.json", {})
    S, C = run / "server", run / "client"
    info = load(S / "info.json", {"lanes": []})
    lanes = info.get("lanes", [])
    single = meta.get("mode") == "single"
    off = float(meta.get("clock_offset", 0.0))   # server_time - client_time
    hosts = {"server": S} if single else {"server": S, "client": C}
    inv = {h: load(d / "inventory.json", {}) for h, d in hosts.items()}
    before = {h: load(d / "before.json", {}) for h, d in hosts.items()}
    after = {h: load(d / "after.json", {}) for h, d in hosts.items()}
    # every time is put on the server clock
    series = {"server": Series(jsonl(S / "samples.jsonl"))}
    if not single:
        series["client"] = Series(jsonl(C / "samples.jsonl"), off)
    marks = [dict(m) for m in jsonl(S / "marks.jsonl")]
    for m in jsonl(C / "marks.jsonl"):
        m["t0"] += off; m["t1"] += off; marks.append(m)
    W = {m["phase"]: (m["t0"], m["t1"]) for m in marks}
    bench = load(C / "bench.json")
    proto = info.get("protocol", "?")
    clients = info.get("client_lanes", lanes)

    # ── 1. setup / environment ──────────────────────────────────────────────────────────────
    sec = "setup"
    add(sec, "mode / profile / protocol", f"{meta.get('mode')} / {meta.get('profile')} / {proto}", "", "INFO",
        f"{len(lanes)} lane(s); mooncake {info.get('mooncake', '?')}; object {info.get('object_bytes')} B")
    if proto != "rdma":
        add(sec, "transport is RDMA", proto, "rdma", "FAIL" if meta.get("protocol_requested") != "tcp" else "WARN",
            "TCP stages every byte through CPU and sockets: not a production fast path")
    if single:
        add(sec, "topology", "single host (RDMA loopback)", "", "INFO",
            "store and load generator share the box: wire numbers are NIC loopback (port counters may not count it), client CPU competes with the leg; "
            "run server + client on two boxes for the real wire")
    if not single and abs(off) > 0.5:
        add(sec, "clock offset server-client", f"{off:+.2f} s", "|x| <= 0.5 s", "WARN", "windows are corrected by this offset; run NTP on both")
    for h, iv in inv.items():
        if not iv:
            add(sec, f"{h} inventory", "missing", "", "FAIL"); continue
        tun = iv.get("tuning", {})
        ml = tun.get("memlock", "")
        add(sec, f"{h} memlock limit", ml, "unlimited", "PASS" if ml == "unlimited" else "WARN", "RDMA registration of the segment pins memory")
        gov = tun.get("governor") or []
        if gov:
            add(sec, f"{h} cpu governor", ",".join(gov), "performance", "PASS" if gov == ["performance"] else "WARN",
                "powersave/ondemand adds wakeup latency to every completion")
        if tun.get("numa_balancing") == "1":
            add(sec, f"{h} numa_balancing", "1", "0", "WARN", "page migration under RDMA-registered memory costs bandwidth")
        if tun.get("io_uring_disabled") not in ("absent", "0", ""):
            add(sec, f"{h} io_uring", f"io_uring_disabled={tun.get('io_uring_disabled')}", "0", "FAIL", "the L3 restore path is io_uring")
        if "iommu" in tun.get("iommu", "") and "pt" not in tun.get("iommu", "") and "off" not in tun.get("iommu", ""):
            add(sec, f"{h} IOMMU mode", tun.get("iommu"), "iommu=pt", "WARN", "translated DMA costs NIC/NVMe throughput")
        sock = iv.get("sockets", 1) or 1
        nd = len(iv.get("dimms", []))
        if nd:
            per = nd / sock
            add(sec, f"{h} DIMMs per socket", f"{per:.0f} ({nd} total)", ">= 8 (all channels)", "PASS" if per >= 8 else "WARN",
                "each empty channel is lost memory bandwidth; the store crosses DRAM 2-3x per byte")
            sizes = {d.get("Size") for d in iv["dimms"]}; spd = {d.get("Configured Memory Speed") for d in iv["dimms"]}
            if len(sizes) > 1 or len(spd) > 1:
                add(sec, f"{h} DIMM population uniform", f"sizes {sorted(sizes)} speeds {sorted(spd)}", "uniform", "WARN", "mixed DIMMs unbalance channels")
            slow = [d for d in iv["dimms"] if num(d.get("Speed")) and num(d.get("Configured Memory Speed")) and num(d["Configured Memory Speed"]) < num(d["Speed"])]
            if slow:
                add(sec, f"{h} DIMMs below rated speed", f"{len(slow)} ({slow[0].get('Configured Memory Speed')} < {slow[0].get('Speed')})", "rated", "WARN")
        fws = {}
        for dev, v in iv.get("nics", {}).items():
            fws.setdefault(v["hca"], set()).add(v["fw"])
            lk = v["pcie"]
            used = any(l.get("dev") == dev for l in (lanes if h == "server" else clients))
            if lk["cur"] != lk["max"]:
                add(sec, f"{h} {dev} PCIe link", f"{lk['cur']} (max {lk['max']})", lk["max"], "FAIL" if used else "WARN", "downtrained: riser, slot or retimer")
            mrr = v.get("lspci", {}).get("max_read_req")
            if used and mrr and mrr < 4096:
                add(sec, f"{h} {dev} PCIe MaxReadReq", f"{mrr} B", ">= 4096 B", "WARN", "small read requests cap RDMA READ / send DMA throughput (setpci or mlxconfig)")
            for nd_, mtu in v.get("mtu", {}).items():
                if used and v["ports"].get("1", {}).get("link_layer") == "Ethernet" and (mtu or 0) < 4200:
                    add(sec, f"{h} {dev} ({nd_}) MTU", mtu, ">= 4200 (RoCE 4096 payload)", "WARN", "a 1500 netdev MTU caps RoCE at 1024-byte frames")
            if used and v.get("active_mtu") and v["active_mtu"] < 4096:
                add(sec, f"{h} {dev} RoCE active_mtu", v["active_mtu"], "4096", "WARN")
            if used and v["pcie"]["numa"] is not None and v["pcie"]["numa"] < 0 and len(iv.get("numa", {})) > 1:
                add(sec, f"{h} {dev} NUMA node", "unknown (-1)", "known", "WARN", "cannot pin the leg next to the NIC")
            for ndv, mod in v.get("module", {}).items():
                if mod.get("alarms_on"):
                    add(sec, f"{h} {dev} optics alarms", "; ".join(mod["alarms_on"][:4]), "none", "FAIL", "transceiver reports alarm/warning flags")
        for hca, fw in fws.items():
            if len(fw) > 1:
                add(sec, f"{h} NIC firmware consistent ({hca})", ",".join(sorted(fw)), "one version", "WARN")
        mods = {}
        for ns, v in iv.get("nvme", {}).items():
            mods.setdefault(v["model"], set()).add(v["fw"])
            if v["pcie"]["cur"] != v["pcie"]["max"]:
                add(sec, f"{h} {ns} PCIe link", f"{v['pcie']['cur']} (max {v['pcie']['max']})", v["pcie"]["max"], "FAIL", "downtrained bay, backplane or cable")
        for m, fw in mods.items():
            if len(fw) > 1:
                add(sec, f"{h} NVMe firmware consistent ({m})", ",".join(sorted(fw)), "one version", "WARN")
        for line in iv.get("dmesg_pcie_limited", [])[:6]:
            add(sec, f"{h} dmesg PCIe bandwidth", line.strip()[-110:], "no 'limited by'", "FAIL" if "limited by" in line else "WARN")
    for l in lanes:
        add(sec, f"lane {l['idx']} server", f"{l.get('dev') or l.get('netdev')} {l['ip']} numa {l.get('numa')} {fmt(l.get('rate_gbit'), '{:.0f}')} Gb/s",
            "", "INFO", f"L3 {l.get('disk_dir')} on {l.get('disk_source')} ({','.join(l.get('disk_nvme') or []) or 'not NVMe'}); "
                        f"segment {l['segment_bytes']/2**30:.0f} GiB, L3 cap {l['disk_cap_bytes']/2**30:.0f} GiB")
        if not l.get("disk_nvme"):
            add(sec, f"lane {l['idx']} L3 on NVMe", l.get("disk_source"), "NVMe-backed", "WARN", "L3 directory is not on NVMe: L3 numbers describe that device")
        if l.get("numa") is not None and l["numa"] >= 0 and l.get("disk_numa") not in (None, -1) and l["disk_numa"] != l["numa"]:
            add(sec, f"lane {l['idx']} NIC/NVMe NUMA locality", f"NIC numa {l['numa']} / NVMe numa {l['disk_numa']}", "same socket", "WARN",
                "every L3 byte crosses the socket interconnect")

    # ── 2. fabric ───────────────────────────────────────────────────────────────────────────
    sec = "fabric (L1<->L2 wire)"
    fab = {}
    for line in (C / "fabric.tsv").read_text().splitlines() if (C / "fabric.tsv").exists() else []:
        p = line.split()
        if len(p) == 2:
            fab[p[0]] = num(p[1])
    ceil_te = {}
    if not fab:
        add(sec, "fabric phase", "not run", "", "SKIP")
    for l in lanes:
        i, line = l["idx"], l.get("rate_gbit")
        for op in ("write", "read"):
            v = fab.get(f"te-{op}-{i}")
            if line and v is not None:
                floor(sec, f"lane {i} Transfer Engine {op} Gb/s", v, TH["te_min_frac_line"] * line, TH["te_warn_frac_line"] * line, "{:.1f}",
                      f"{v / line:.0%} of {line:.0f} Gb/s line rate (64 KiB slices, the store's wire message)")
            elif v is not None:
                add(sec, f"lane {i} Transfer Engine {op} Gb/s", fmt(v, "{:.1f}"), "line rate unknown", "INFO")
            elif fab:
                add(sec, f"lane {i} Transfer Engine {op}", "no result", "a number", "FAIL", f"see client/fabric-te-{op}-{i}.log")
            if v:
                ceil_te[(i, op)] = v / 8
            pv = fab.get(f"perftest-{op}-{i}")
            if pv is not None and line:
                floor(sec, f"lane {i} perftest ib_{op}_bw Gb/s", pv, TH["perftest_min_frac_line"] * line, None, "{:.1f}",
                      f"{pv / line:.0%} of line; raw verbs reference" + (f"; Mooncake reaches {v / pv:.0%} of it" if v else ""))
                if v and v < 0.85 * pv:
                    add(sec, f"lane {i} Mooncake vs raw verbs ({op})", f"{v / pv:.0%}", ">= 85%", "WARN", "gap is software (GID/QP count/slicing), not the NIC")
    if len(lanes) > 1:
        for op in ("write", "read"):
            both = sum(fab.get(f"te-{op}-all-{l['idx']}") or 0 for l in lanes)
            alone = sum(fab.get(f"te-{op}-{l['idx']}") or 0 for l in lanes)
            if alone:
                floor(sec, f"all lanes at once, {op} Gb/s", both, TH["all_lanes_min_frac_sum"] * alone, None, "{:.1f}",
                      f"{both / alone:.0%} of the lanes' sum alone: < 90% = shared PCIe slot, socket link or memory bandwidth")
    for l in lanes:
        win = W.get(f"fabric:te-write-{l['idx']}")
        if win:
            bn = analyze(series, win, inv, lanes, clients, info, single, ceilings={}, kind="fabric")
            if bn:
                bottlenecks.append((f"fabric TE write lane {l['idx']}", bn))

    # ── 3. DRAM ─────────────────────────────────────────────────────────────────────────────
    sec = "L2 medium: DRAM bandwidth"
    stream = {}
    for line in (S / "stream.tsv").read_text().splitlines() if (S / "stream.tsv").exists() else []:
        p = line.split()
        if len(p) == 3:
            stream[(p[0], p[1])] = num(p[2])
    if not stream:
        add(sec, "STREAM triad", "not run", "", "SKIP")
    for (n, m), v in sorted(stream.items()):
        nic_gbs = sum((l.get("rate_gbit") or 0) / 8 for l in lanes if str(l.get("numa")) == n) or max((l.get("rate_gbit") or 0) / 8 for l in lanes) if lanes else 0
        if n == m:
            floor(sec, f"triad node {n} local GB/s", v, TH["dram_local_x_nic"] * nic_gbs if nic_gbs else 0, None, "{:.1f}",
                  f"needs {TH['dram_local_x_nic']}x the {nic_gbs:.0f} GB/s of wire on this node: RDMA in, bucket write, restore read, RDMA out")
        else:
            floor(sec, f"triad cpu {n} -> mem {m} remote GB/s", v, TH["dram_remote_x_nic"] * nic_gbs if nic_gbs else 0, None, "{:.1f}",
                  "what a NIC or leg on the wrong socket pays")

    # ── 4. raw NVMe ─────────────────────────────────────────────────────────────────────────
    sec = "L3 medium: raw NVMe"
    raw = {}
    for tag in ("seqread", "objread", "seqwrite"):
        js = load(S / "nvme" / f"raw-{tag}.json")
        if js:
            for j in js.get("jobs", []):
                side = "write" if tag == "seqwrite" else "read"
                raw.setdefault(tag, {})[j["jobname"]] = {"gbps": j[side]["bw_bytes"] / 1e9, "iops": j[side]["iops"],
                    "lat_p99_ms": (j[side].get("clat_ns", {}).get("percentile", {}).get("99.000000", 0) or 0) / 1e6, "err": j.get("error", 0)}
    if not raw:
        add(sec, "raw NVMe", "not run (no NVMe or skipped)", "", "SKIP")
    for tag, d in raw.items():
        med = statistics.median(v["gbps"] for v in d.values()) if d else 0
        for drv, v in sorted(d.items(), key=lambda kv: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", kv[0])]):
            floor(sec, f"{drv} {tag} GB/s", v["gbps"], TH["drive_outlier_frac"] * med, None, "{:.2f}",
                  f"median of {len(d)} drives {med:.2f}; p99 {v['lat_p99_ms']:.1f} ms" + (f"; fio error {v['err']}" if v["err"] else ""))
            if v["err"]:
                add(sec, f"{drv} {tag} I/O errors", v["err"], "0", "FAIL")
        add(sec, f"aggregate {tag} GB/s ({len(d)} drives at once)", fmt(sum(v["gbps"] for v in d.values())), "", "INFO",
            "per-drive numbers come from the run that loads every drive together")

    # ── 5. filesystem ceilings ──────────────────────────────────────────────────────────────
    sec = "L3 path: filesystem ceilings"
    fs_ceiling = {}
    for l in lanes:
        i = l["idx"]
        for tag, side, what in (("bufwrite", "write", "buffered 1 GiB files + fdatasync (the bucket writer's pattern)"),
                                ("dirwrite", "write", "O_DIRECT 1 MiB writes (the device through the filesystem)"),
                                ("objread", "read", "O_DIRECT object-size reads QD32 (the restore pattern)")):
            js = load(S / "nvme" / f"fs-{tag}-{i}.json")
            if not js:
                continue
            jobs = js.get("jobs", [])
            g = sum(j[side]["bw_bytes"] for j in jobs) / 1e9
            errs = sum(j.get("error", 0) for j in jobs)
            fs_ceiling[(i, tag)] = g
            add(sec, f"lane {i} {tag} GB/s", fmt(g), "ceiling", "INFO" if not errs else "FAIL", what + (f"; fio errors {errs}" if errs else ""))
        bw, dw = fs_ceiling.get((i, "bufwrite")), fs_ceiling.get((i, "dirwrite"))
        if bw and dw and bw < 0.5 * dw:
            add(sec, f"lane {i} buffered vs direct write", f"{bw / dw:.0%}", ">= 50%", "WARN",
                "page cache / dirty writeback is the L3 write limit, not the drives (memory bandwidth or dirty_* settings)")

    # ── 6. store ────────────────────────────────────────────────────────────────────────────
    sec = "store (real Mooncake: observed payload rates; tier placement requires telemetry)"
    if not bench:
        add(sec, "store phase", "not run" if not (C / "bench.log").exists() else "bench crashed (see client/bench.log)", "",
            "SKIP" if not (C / "bench.log").exists() else "FAIL")
    else:
        P = bench["phases"]; cfgb = bench["config"]
        W_ = cfgb["workers"]
        for l in lanes:
            i, si = l["idx"], str(l["idx"])
            line_gbs = (l.get("rate_gbit") or 0) / 8
            te_w = ceil_te.get((i, "write")) or line_gbs or None
            te_r = ceil_te.get((i, "read")) or line_gbs or None
            put = P["put"][si]
            floor(sec, f"lane {i} initial put burst GB/s", put["burst_gbps"], TH["store_put_burst_frac_te"] * te_w if te_w else 0,
                  TH["store_put_burst_warn_frac_te"] * te_w if te_w else None, "{:.2f}",
                  f"until half the segment is written; ceiling = Transfer Engine write {fmt(te_w)} GB/s")
            fsw = fs_ceiling.get((i, "bufwrite"))
            floor(sec, f"lane {i} post-fill put churn GB/s", put["churn_gbps"], TH["store_churn_frac_fs_write"] * fsw if fsw else 0, None, "{:.2f}",
                  f"post-fill interval; verify persistence and eviction counters; reference = buffered fs write {fmt(fsw)} GB/s; "
                  f"refusal ratio {put['refusal_ratio']:.2f}, {put['refusal_wait_frac']:.0%} of worker time waiting on a full segment")
            add(sec, f"lane {i} put overall GB/s", fmt(put["gbps"]), "", "INFO",
                f"{put['bytes'] / 1e9:.0f} GB in {put['seconds']:.0f} s; p50 {put['lat_ms']['p50']:.1f} ms p99 {put['lat_ms']['p99']:.1f} ms p99.9 {put['lat_ms']['p999']:.1f} ms per batch of {cfgb['batch']}")
            if put["refusal_ratio"] > 0.5:
                notes.append(f"lane {i}: {put['refusal_ratio']:.1f} put refusals per object: the DRAM segment ({l['segment_bytes']/2**30:.0f} GiB) "
                             "fills with objects pinned until persisted; sustained write rate ~ segment / persist latency. More RAM per leg raises it.")
            sweep = sorted((int(k.split("@")[1]), v[si]) for k, v in P.items() if k.startswith("get_dram@"))
            if sweep:
                peak_c, peak = max(sweep, key=lambda x: x[1]["gbps"])
                floor(sec, f"lane {i} hot candidate get GB/s (best tested)", peak["gbps"], TH["store_get_dram_frac_te"] * te_r if te_r else 0,
                      TH["store_get_dram_warn_frac_te"] * te_r if te_r else None, "{:.2f}",
                      f"at {peak_c} workers; scaling " + ", ".join(f"{c}w {r['gbps']:.1f}" for c, r in sweep) + f"; ceiling = TE read {fmt(te_r)} GB/s")
                p50, p999 = peak["lat_ms"]["p50"], peak["lat_ms"]["p999"]
                add(sec, f"lane {i} L2 read latency (batch of {cfgb['batch']})", f"p50 {p50:.2f} / p99 {peak['lat_ms']['p99']:.2f} / p99.9 {p999:.2f} / max {peak['lat_ms']['max']:.1f} ms",
                    f"p99.9 <= {TH['tail_p999_over_p50_warn']:.0f}x p50", "PASS" if p999 <= TH["tail_p999_over_p50_warn"] * p50 else "WARN",
                    "single-worker: " + f"p50 {sweep[0][1]['lat_ms']['p50']:.2f} ms ({cfgb['batch'] * cfgb['object_bytes'] / max(sweep[0][1]['lat_ms']['p50'], 1e-6) / 1e6:.2f} GB/s per stream)")
                miss = sum(r["misses"] for _, r in sweep)
                reads = sum(r["objs"] + r["misses"] for _, r in sweep)
                mfr = miss / max(reads, 1)
                add(sec, f"lane {i} L2 read misses (newest objects)", f"{miss} of {reads} ({mfr:.1%})", f"<= {TH['nvme_miss_frac_max']:.0%}",
                    "PASS" if mfr <= TH["nvme_miss_frac_max"] else "FAIL",
                    "numeric tail cohort; concurrent insertion order and eviction can change retention; misses do not prove lost writes")
                if len(sweep) > 1 and sweep[-1][1]["gbps"] < 1.1 * sweep[0][1]["gbps"] and sweep[-1][0] > sweep[0][0]:
                    notes.append(f"lane {i}: DRAM reads do not scale with workers ({sweep[0][1]['gbps']:.1f} -> {sweep[-1][1]['gbps']:.1f} GB/s): one serialized resource")
            gn = P["get_nvme"][si]
            rceil = min(x for x in (fs_ceiling.get((i, "objread")), te_r) if x) if (fs_ceiling.get((i, "objread")) or te_r) else None
            floor(sec, f"lane {i} L3 candidate get GB/s", gn["gbps"], TH["store_get_nvme_frac_ceiling"] * rceil if rceil else 0,
                  TH["store_get_nvme_warn_frac_ceiling"] * rceil if rceil else None, "{:.2f}",
                  f"ceiling = min(fs object read {fmt(fs_ceiling.get((i, 'objread')))}, TE read {fmt(te_r)}) GB/s; "
                  f"p50 {gn['lat_ms']['p50']:.1f} p99 {gn['lat_ms']['p99']:.1f} p99.9 {gn['lat_ms']['p999']:.1f} ms")
            got = gn["objs"] + gn["misses"]
            mf = gn["misses"] / max(got, 1)
            add(sec, f"lane {i} L3 read misses", f"{gn['misses']} of {got} ({mf:.1%})", f"<= {TH['nvme_miss_frac_max']:.0%}",
                "PASS" if mf <= TH["nvme_miss_frac_max"] else "FAIL", "mid-fill candidate cohort; placement and retention require per-window restore/eviction evidence")
            # did those bytes really come off the disks?
            dsk = l.get("disk_nvme") or []
            win = W.get("store:get_nvme")
            if win and dsk and gn["bytes"]:
                st = series["server"].stats(win[0], win[1], inv.get("server"), lanes)
                if st:
                    shared = [x for x in lanes if set(x.get("disk_nvme") or []) & set(dsk)]
                    tot_bytes = sum(P["get_nvme"][str(x["idx"])]["bytes"] for x in shared)
                    rd_bytes = sum(st["disk"].get(d, {}).get("read", 0) for d in dsk) * st["dt"] * 1e9
                    att = rd_bytes / max(tot_bytes, 1)
                    floor(sec, f"lane {i} L3 read attribution (disk bytes / bytes served)", att, TH["l3_read_attribution_min"], None, "{:.2f}",
                          "below = the 'NVMe' gets were served from DRAM or page cache: the L3 number is not an L3 number")
            if "mixed" in P:
                m = P["mixed"][si]
                add(sec, f"lane {i} mixed: put + get GB/s", f"{m['put']['gbps']:.2f} + {m['get']['gbps']:.2f}", "", "INFO",
                    f"{W_ - W_ // 2} readers next to {W_ // 2} writers: get p99 {m['get']['lat_ms']['p99']:.1f} ms "
                    f"(reads alone at {sweep[-1][0]} workers: p99 {sweep[-1][1]['lat_ms']['p99']:.1f} ms, {sweep[-1][1]['gbps']:.2f} GB/s); misses {m['get']['misses']}")
            ex = P["exist"][si]
            add(sec, f"lane {i} full capacity reached (oldest 5% evicted)", f"{ex['head_evicted_frac']:.2f}", "> 0",
                "PASS" if ex["head_evicted_frac"] > 0 else ("WARN" if cfgb["fill_ratio"] >= 1 else "SKIP"),
                "disk-watermark eviction ran: the whole capacity path was exercised")
            errs = {}
            mism = 0
            for k, v in P.items():
                for part in (v[si].values() if k == "mixed" else [v[si]]):
                    for e, c in part["errors"].items():
                        errs[e] = errs.get(e, 0) + c
                    mism += part["mismatch"]
            add(sec, f"lane {i} store errors", ", ".join(f"rc {k} x{v}" for k, v in errs.items()) or "0", "0", "FAIL" if errs else "PASS",
                "-200 after the 600 s retry budget, transfer failures (-800s), RPC failures (-900s), INTERNAL_ERROR (-1)")
            add(sec, f"lane {i} content verification mismatches", mism, "0", "FAIL" if mism else "PASS", "sampled 4 KiB at 4 points of every object read")
        # leg / master logs
        for l in lanes:
            i = l["idx"]
            leg = (S / "store" / f"leg-{i}.log").read_text(errors="replace") if (S / "store" / f"leg-{i}.log").exists() else ""
            mst = (S / "store" / f"master-{i}.log").read_text(errors="replace") if (S / "store" / f"master-{i}.log").exists() else ""
            if proto == "rdma":
                ok = "installTransport, type=rdma" in leg or "type=rdma" in leg
                tcp = "TcpTransport: listen" in leg
                add(sec, f"lane {i} leg transport", f"rdma={ok} tcp={tcp}", "rdma, no tcp", "PASS" if ok and not tcp else "FAIL",
                    "from the leg log: the transport the leg actually installed")
            if "io_uring_queue_init failed" in leg:
                add(sec, f"lane {i} leg io_uring restore path", "io_uring_queue_init failed", "io_uring", "FAIL", "restores silently degrade to POSIX reads")
            for name, txt in (("leg", leg), ("master", mst)):
                fatal = [x for x in txt.splitlines() if re.match(r"^F\d{4}", x) or "Check failed" in x or "Segmentation fault" in x or "core dumped" in x]
                errl = [x for x in txt.splitlines() if re.match(r"^E\d{4}", x) and "Local segment descriptor not found" not in x]
                if fatal:
                    add(sec, f"lane {i} {name} log FATAL", len(fatal), "0", "FAIL", fatal[0][-160:])
                if errl:
                    distinct = {}
                    for x in errl:
                        k = re.sub(r"\d+", "N", re.sub(r"^E\d{4} [\d:.]+ +\d+ ", "", x))[:120]
                        distinct[k] = distinct.get(k, 0) + 1
                    top = sorted(distinct.items(), key=lambda kv: -kv[1])[:3]
                    add(sec, f"lane {i} {name} log ERROR lines", len(errl), "0", "WARN", " | ".join(f"{c}x {k}" for k, c in top))
        for l in lanes:
            master_metrics(sec, l, S / "store" / f"master-{l['idx']}.metrics", P, str(l["idx"]))
        alive = load(S / "alive_at_finish.json", {})
        for k, v in alive.items():
            if not v and (k.startswith("leg") or k.startswith("master")):
                add(sec, f"{k} process alive at end", "died", "alive", "FAIL", "the store process exited during the run (crash / OOM-kill: see dmesg)")
        # bottlenecks per store phase
        ce = {"fs": fs_ceiling, "raw": raw, "te": ceil_te}
        for ph in ["put", "store:put_churn", "get_dram@" + str(W_), "get_nvme", "mixed"]:
            key = ph if ph.startswith("store:") else f"store:{ph}"
            wins = [v for k, v in W.items() if k == key or (ph == "store:put_churn" and k.startswith("store:put_churn"))]
            if not wins:
                continue
            win = (min(w[0] for w in wins), max(w[1] for w in wins))
            extra = {}
            if ph in ("put", "store:put_churn"):
                extra["refusal_wait_frac"] = max(P["put"][str(l["idx"])]["refusal_wait_frac"] for l in lanes)
            bn = analyze(series, win, inv, lanes, clients, info, single, ce, kind=ph, extra=extra)
            if bn:
                bottlenecks.append((ph.replace("store:", ""), bn))

    # ── 7. errors and counters ──────────────────────────────────────────────────────────────
    for h in hosts:
        errors_section(h, before.get(h) or {}, after.get(h) or {}, inv.get(h) or {}, series.get(h), W)

    # ── output ──────────────────────────────────────────────────────────────────────────────
    peak_lines = ["", "Observed payload bandwidth (GB/s; best tested concurrency, not certified hardware peak)",
                  "lane workload             successful offered workers seconds misses errors mismatch final_refused status"]
    for li in (bench or {}).get("config", {}).get("lanes", []):
        lane_id = str(li["idx"])
        for label, prefix in (("sustained put", "put_sustained@"), ("hot get", "get_dram@"), ("L3 candidate get", "get_nvme")):
            candidates = [values[lane_id] for name, values in bench.get("phases", {}).items()
                          if name.startswith(prefix) and lane_id in values]
            if candidates:
                clean = [r for r in candidates if not r["errors"] and not r["misses"] and not r["mismatch"] and r["bytes"] > 0]
                r = max(clean or candidates, key=lambda x: x["gbps"])
                status = "clean observed" if clean else "invalid/errorful"
                peak_lines.append(f"{lane_id:>4} {label:<20} {r['gbps']:9.3f} {r.get('offered_gbps', r['gbps']):7.3f} {r['workers']:7} {r['seconds']:7.1f} {r['misses']:6} {sum(r['errors'].values()):6} {r['mismatch']:8} {r.get('deadline_refused', 0):13} {status}")
        mixed = bench.get("phases", {}).get("mixed", {}).get(lane_id, {})
        for direction, r in mixed.items():
            status = "clean observed" if not r["errors"] and not r["misses"] and not r["mismatch"] and r["bytes"] > 0 else "invalid/errorful"
            peak_lines.append(f"{lane_id:>4} {'mixed ' + direction:<20} {r['gbps']:9.3f} {r.get('offered_gbps', r['gbps']):7.3f} {r['workers']:7} {r['seconds']:7.1f} {r['misses']:6} {sum(r['errors'].values()):6} {r['mismatch']:8} {r.get('deadline_refused', 0):13} {status}")
        if mixed:
            peak_lines.append(f"     mixed aggregate      {sum(r['gbps'] for r in mixed.values()):9.3f} (shared measurement window)")
    peak_lines.extend(["Qualification: observed only; verify cross-host RDMA counters and per-window L3 restores.",
                       "Hot/L3 cohorts are placement candidates; mixed gets use a bounded ring of recently accepted puts.",
                       "Integrity checks sample four 4 KiB spans; latency is per batch, not per object."])
    out = "\n".join(peak_lines) + "\n\n" + render(run, meta, info)
    (run / "report.txt").write_text(re.sub(r"\033\[[0-9;]*m", "", out))
    json.dump({"rows": rows, "bottlenecks": [{"phase": p, **b} for p, b in bottlenecks], "notes": notes, "thresholds": TH},
              open(run / "report.json", "w"), indent=1, default=str)
    print(out)
    return 1 if any(r["verdict"] == "FAIL" for r in rows) else 0


def num(x):
    if x is None:
        return None
    m = re.match(r"\s*(-?[\d.]+)", str(x))
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def prom(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-\d.eE+naNinf]+)", line)
        if m:
            try:
                out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
            except ValueError:
                pass
    return out


def master_metrics(sec, l, path, P, si):
    """The master's own view of the run: allocation refusals, eviction efficiency, lost gets, disk usage."""
    m = prom(path)
    i = l["idx"]
    if not m:
        add(sec, f"lane {i} master metrics", "unavailable", "", "SKIP"); return
    g = lambda k: m.get(k, 0.0)
    puts = g("master_batch_put_start_items_total") + g("master_put_start_requests_total")
    af = g("master_put_start_alloc_failures_total")
    add(sec, f"lane {i} master: put allocation failures", f"{af:.0f} of {puts:.0f} PutStart items", "reported", "INFO",
        "every one is a put the DRAM segment had no room for (the bench retried it)")
    att, ok = g("master_attempted_evictions_total"), g("master_successful_evictions_total")
    if att:
        eff = ok / att
        add(sec, f"lane {i} master: DRAM eviction success", f"{ok:.0f}/{att:.0f} ({eff:.0%})", ">= 50%", "PASS" if eff >= 0.5 else "WARN",
            f"evicted {g('master_evicted_size_bytes_mem') / 1e9:.1f} GB / {g('master_evicted_key_count_mem'):.0f} keys from DRAM; "
            "a low ratio = eviction rounds find nothing evictable because objects are pinned until the leg persists them: L3 write latency is gating L2")
    gf = g("master_batch_get_replica_list_failed_items_total") + g("master_get_replica_list_failures_total")
    gets = sum(v[si]["objs"] + v[si]["misses"] for k, v in P.items() if k.startswith("get_"))
    if gf:
        add(sec, f"lane {i} master: gets for keys it no longer has", f"{gf:.0f}", "0", "WARN",
            f"of ~{gets} gets; objects evicted from DRAM and not (or no longer) on disk: a cache miss the engine would recompute")
    fsz = g("master_allocated_file_size_bytes")
    if fsz:
        add(sec, f"lane {i} master: L3 bytes held at end", f"{fsz / 2**30:.1f} GiB of {l['disk_cap_bytes'] / 2**30:.0f} GiB cap",
            "<= cap", "PASS" if fsz <= l["disk_cap_bytes"] * 1.01 else "FAIL", "the disk tier stayed inside its watermark band")
    dr = g("master_evict_disk_replica_requests_total")
    if dr:
        add(sec, f"lane {i} master: disk-replica eviction requests", f"{dr:.0f}", "reported", "INFO", "the disk watermark fired")


# ── bottleneck attribution ──────────────────────────────────────────────────────────────────
def analyze(series, win, inv, lanes, clients, info, single, ceilings, kind, extra=None):
    """Score every resource by utilization of its own ceiling during the window; the max is the bottleneck."""
    cands, facts = [], []
    for h, ser in series.items():
        st = ser.stats(win[0], win[1], inv.get(h), lanes)
        if not st:
            continue
        mine = lanes if h == "server" else clients
        for l in mine:
            key = l.get("dev") or l.get("netdev")
            v = st["nic"].get(key)
            if v and l.get("rate_gbit"):
                lr = l["rate_gbit"] / 8
                u = max(v["tx"], v["rx"]) / lr
                cands.append((u, f"{h} NIC {key} wire", f"tx {v['tx']:.2f} rx {v['rx']:.2f} GB/s of {lr:.1f} line ({u:.0%})"))
        if h == "server":
            disks = sorted({d for l in lanes for d in (l.get("disk_nvme") or [])})
            raw = ceilings.get("raw", {}) if ceilings else {}
            agg_r = sum(st["disk"].get(d, {}).get("read", 0) for d in disks)
            agg_w = sum(st["disk"].get(d, {}).get("write", 0) for d in disks)
            for d in disks:
                dv = st["disk"].get(d)
                if not dv:
                    continue
                cr = raw.get("objread", {}).get(d, {}).get("gbps") or raw.get("seqread", {}).get(d, {}).get("gbps")
                cw = raw.get("seqwrite", {}).get(d, {}).get("gbps")
                ur = dv["read"] / cr if cr else 0
                uw = dv["write"] / cw if cw else 0
                u = max(ur, uw, dv["util"] * 0.7)   # io_ticks saturates at 1 with QD1 on NVMe: weak evidence
                cands.append((u, f"server NVMe {d}", f"r {dv['read']:.2f} w {dv['write']:.2f} GB/s, busy {dv['util']:.0%}, in-flight {dv['inflight']:.1f}"
                              + (f", {ur:.0%} of its read ceiling" if cr else "") + (f", {uw:.0%} of its write ceiling" if cw else "")))
            fs = ceilings.get("fs", {}) if ceilings else {}
            for l in lanes:
                i = l["idx"]
                ld = l.get("disk_nvme") or []
                lr_ = sum(st["disk"].get(d, {}).get("read", 0) for d in ld); lw = sum(st["disk"].get(d, {}).get("write", 0) for d in ld)
                for tag, val, what in (("objread", lr_, "read"), ("bufwrite", lw, "write")):
                    c = fs.get((i, tag))
                    if c and val > 0.05:
                        cands.append((val / c, f"server lane {i} L3 filesystem {what}", f"{val:.2f} of {c:.2f} GB/s fio ceiling ({val / c:.0%})"))
            if disks:
                facts.append(f"L3 disks: read {agg_r:.2f} GB/s, write {agg_w:.2f} GB/s total")
            m = st["mem"]
            facts.append(f"dirty+writeback peak {m['dirty_wb_max_gb']:.1f} GB, MemAvailable min {m['avail_min_gb']:.1f} GB")
            if m["avail_min_gb"] < 2:
                cands.append((0.95, "server memory capacity", f"MemAvailable fell to {m['avail_min_gb']:.1f} GB"))
        for n, b in st["cpu_node"].items():
            if b is not None:
                cands.append((b, f"{h} CPU node {n}", f"{b:.0%} busy"))
        for t in st["threads"][:3]:
            if t["cpu"] > 0.5:
                cands.append((t["cpu"], f"{h} thread '{t['comm']}' ({t['tag']})", f"{t['cpu']:.0%} of one core: single-thread bound at 100%"))
        bw = st["procs"].get("bench", [])
        if bw:
            workers = sorted(bw)[-max(1, len(bw) - 1):]   # drop the coordinator
            avg = sum(workers) / len(workers)
            cands.append((avg, f"{h} load generator workers", f"{avg:.0%} of a core each on average ({len(workers)} procs)"))
        for r, v in st["psi"].items():
            if v["some"] > 0.2:
                facts.append(f"{h} PSI {r}: some {v['some']:.0%} full {v['full']:.0%} of the window stalled")
            if r == "io" and v["full"] > 0.3:
                cands.append((v["full"] + 0.4, f"{h} I/O stall (PSI)", f"all tasks stalled on I/O {v['full']:.0%} of the time"))
        hot = sorted(((k, v) for k, v in st["temp_max"].items() if v > 60), key=lambda kv: -kv[1])[:3]
        if hot:
            facts.append(f"{h} hottest sensors: " + ", ".join(f"{k.split(':')[0]}:{k.split(':')[-1]} {v:.0f}C" for k, v in hot))
        tk = ", ".join(f"{t['comm']}({t['tag']}) {t['cpu']:.0%}" for t in st["threads"][:4])
        if tk:
            facts.append(f"{h} hottest threads: {tk}")
    if extra and extra.get("refusal_wait_frac", 0) > 0.05:
        f = extra["refusal_wait_frac"]
        cands.append((min(1.0, 0.5 + f), "server DRAM segment capacity (put refusals)",
                      f"workers spent {f:.0%} of their time re-offering puts the full segment refused: persistence latency x segment size bounds the write rate"))
    if not cands:
        return None
    cands.sort(key=lambda c: -c[0])
    top = cands[0]
    if top[0] >= TH["bottleneck_saturated"]:
        verdict = f"SATURATED: {top[1]}"
    elif top[0] >= TH["bottleneck_bound"]:
        verdict = f"likely bound by {top[1]}"
    else:
        verdict = "no resource above 60%: latency/concurrency bound (RPC round trips, serialization); raise --workers/--batch to find the hardware limit"
    return {"verdict": verdict, "top": [{"score": round(c[0], 3), "resource": c[1], "evidence": c[2]} for c in cands[:4]], "facts": facts}


# ── error counters ──────────────────────────────────────────────────────────────────────────
ERR_PAT = re.compile(r"(crc|symbol_err|fcs|align|jabber|undersize|oversize|fragment|_err|error|discard|drop|out_of_buffer|uncorrect|"
                     r"buffer_overrun|link_down|link_error|integrity|constraint|out_of_sequence|packet_seq|ack_timeout|rnr_nak|"
                     r"implied_nak|duplicate_request|slow_restart|retrans|stuck|bad_shorted|high_temp|high_power|overflow|timeout)", re.I)
CONG_PAT = re.compile(r"(pause|cnp|ecn|buffer_passed_thres|congest)", re.I)
CORR_PAT = re.compile(r"(corrected|err_lane_\d+|fec_corr)", re.I)
BENIGN = re.compile(r"^(rx|tx|ch)\d+_|_cache_|xdp|^rx_vport|^tx_vport|lro|csum|tso|gso|_wake|_stopped|_nop|dropped_xdp|_arfs|tls|ptp|_xsk|_wqe_err$", re.I)


def errors_section(h, b, a, inv, ser, W):
    sec = f"errors: {h}"
    if not a:
        add(sec, "after-snapshot", "missing", "", "FAIL", "the run did not reach teardown; counters cannot be compared"); return
    # PCIe AER
    roles = {}
    for dev, v in inv.get("nics", {}).items():
        for i, bdf in enumerate(v["pcie"]["chain"]):
            roles[bdf] = f"{dev}" + ("" if bdf == v["pcie"]["bdf"] else " upstream port")
    for ns, v in inv.get("nvme", {}).items():
        for bdf in v["pcie"]["chain"]:
            roles.setdefault(bdf, f"{ns}" + ("" if bdf == v["pcie"]["bdf"] else " upstream port"))
    aer_moved = 0
    for bdf, rec in a.get("aer", {}).items():
        pb = b.get("aer", {}).get(bdf, {})
        for kind in ("correctable", "nonfatal", "fatal"):
            for name, val in rec.get(kind, {}).items():
                if name.startswith("TOTAL"):
                    continue
                d = val - pb.get(kind, {}).get(name, 0)
                if d > 0:
                    aer_moved += 1
                    role = roles.get(bdf, "other device")
                    v = "FAIL" if kind != "correctable" else (strict("x") if bdf in roles else "WARN")
                    add(sec, f"PCIe AER {kind} {name} @ {bdf} ({role})", f"+{d}", "0", v,
                        {"RxErr": "receiver errors: marginal link / signal integrity", "BadTLP": "corrupted TLPs (CRC) retried",
                         "BadDLLP": "link-layer CRC errors", "Rollover": "replay counter rollover", "Timeout": "replay timer timeouts: link retraining",
                         "CorrIntErr": "corrected internal error", "HeaderOF": "header log overflow"}.get(name, "") +
                        (" - happened DURING the load" if kind == "correctable" else " - uncorrectable: data path integrity"))
                elif val and kind != "correctable" and bdf in roles:
                    add(sec, f"PCIe AER {kind} {name} @ {bdf} ({roles[bdf]}) history", val, "0", "WARN", "nonzero before the run")
        for name, val in rec.get("rootport", {}).items():
            d = val - pb.get("rootport", {}).get(name, 0)
            if d > 0:
                aer_moved += 1
                add(sec, f"PCIe root port {name} @ {bdf}", f"+{d}", "0", "FAIL" if "fatal" in name else strict("x"))
    if not a.get("aer"):
        add(sec, "PCIe AER counters", "not exposed (kernel without aer_dev_* sysfs)", "", "SKIP")
    elif not aer_moved:
        add(sec, "PCIe AER (correctable + uncorrectable, every device)", "0 new", "0", "PASS")
    # PCIe link change during the run
    for dev, lk in a.get("link", {}).items():
        if dev in b.get("link", {}) and b["link"][dev] != lk:
            add(sec, f"PCIe link of {dev} changed during the run", f"{b['link'][dev]} -> {lk}", "stable", "FAIL", "the link retrained under load")
    # EDAC
    ec_moved = 0
    for mc, rec in a.get("edac", {}).items():
        pb = b.get("edac", {}).get(mc, {})
        for k, v in rec.items():
            if isinstance(v, dict):
                for kk in ("ce", "ue"):
                    d = (v.get(kk) or 0) - ((pb.get(k) or {}).get(kk) or 0)
                    if d > 0:
                        ec_moved += 1
                        add(sec, f"memory {kk.upper()} on {k.split(':', 1)[1]} ({mc})", f"+{d}", "0", "FAIL" if kk == "ue" else strict("x"),
                            "correctable ECC errors under load = a failing DIMM, slot or memory training" if kk == "ce" else "uncorrectable memory error")
            elif v is not None:
                d = v - (pb.get(k) or 0)
                if d > 0 and not any(isinstance(x, dict) and x.get(k.split("_")[0]) for x in rec.values()):
                    ec_moved += 1
                    add(sec, f"memory {k} ({mc})", f"+{d}", "0", "FAIL" if k.startswith("ue") else strict("x"))
                elif v and k.startswith(("ce_count", "ue_count")):
                    add(sec, f"memory {k} ({mc}) since boot", v, "0", "WARN", "errors before this run")
    if not a.get("edac"):
        add(sec, "memory ECC (EDAC)", "no EDAC driver loaded: memory errors are INVISIBLE", "edac mc present", "WARN",
            "load the platform EDAC module (e.g. amd64_edac / skx_edac / i10nm_edac) or check the BMC SEL")
    elif not ec_moved:
        add(sec, "memory ECC (EDAC CE + UE)", "0 new", "0", "PASS")
    # NVMe SMART / error log
    for c, s in a.get("nvme", {}).items():
        p = b.get("nvme", {}).get(c, {})
        dd = lambda k: (s.get(k) or 0) - (p.get(k) or 0)
        bad = []
        if dd("media_errors") > 0:
            bad.append(("media/data integrity errors", f"+{dd('media_errors')}", "FAIL"))
        if dd("num_err_log_entries") > 0:
            bad.append(("error-log entries", f"+{dd('num_err_log_entries')}", strict("x")))
        if s.get("critical_warning"):
            bad.append(("critical_warning", s["critical_warning"], "FAIL"))
        if dd("warning_temp_time") > 0:
            bad.append(("minutes above warning temp", f"+{dd('warning_temp_time')}", "FAIL"))
        if dd("critical_comp_time") > 0:
            bad.append(("minutes above critical temp", f"+{dd('critical_comp_time')}", "FAIL"))
        thr = sum(dd(k) for k in s if re.match(r"thm_temp\d_total_time", k))
        trn = sum(dd(k) for k in s if re.match(r"thm_temp\d_trans_count", k))
        if thr > 0 or trn > 0:
            bad.append(("thermal throttling", f"+{thr} s, +{trn} transitions", "FAIL"))
        if dd("unsafe_shutdowns") > 0:
            bad.append(("unsafe shutdowns", f"+{dd('unsafe_shutdowns')}", "FAIL"))
        spare, thr_s = s.get("avail_spare"), s.get("spare_thresh")
        if spare is not None and thr_s is not None and spare <= thr_s:
            bad.append(("available spare at/below threshold", f"{spare}% <= {thr_s}%", "FAIL"))
        if (s.get("percent_used") or 0) >= TH["percent_used_warn"]:
            bad.append(("endurance used", f"{s['percent_used']}%", "WARN"))
        if (s.get("media_errors") or 0) > 0 and not dd("media_errors"):
            bad.append(("media errors since manufacture", s["media_errors"], "WARN"))
        temp = s.get("temperature")
        if temp:
            tc = temp - 273 if temp > 200 else temp
            if tc >= TH["nvme_temp_warn_c"]:
                bad.append(("composite temperature at end", f"{tc:.0f} C", "WARN"))
        for what, val, v in bad:
            add(sec, f"NVMe {c} {what}", val, "0", v)
        if not bad:
            add(sec, f"NVMe {c} SMART + error log", f"clean (used {s.get('percent_used', '?')}%, spare {s.get('avail_spare', '?')}%, "
                f"written this run {((s.get('data_units_written') or 0) - (p.get('data_units_written') or 0)) * 512e3 / 1e9:.0f} GB)", "0 new", "PASS")
    # RDMA port + hw counters
    for k, rec in a.get("rdma", {}).items():
        pb = b.get("rdma", {}).get(k, {})
        moved = []
        for grp in ("counters", "hw"):
            for name, v in rec.get(grp, {}).items():
                if v is None or name in ("port_xmit_data", "port_rcv_data", "port_xmit_packets", "port_rcv_packets", "unicast_xmit_packets",
                                         "unicast_rcv_packets", "multicast_xmit_packets", "multicast_rcv_packets", "port_xmit_wait"):
                    continue
                d = v - (pb.get(grp, {}).get(name) or 0)
                if d <= 0:
                    continue
                if CONG_PAT.search(name):
                    moved.append((name, d, "WARN", "congestion signal: a receiver could not keep up"))
                elif ERR_PAT.search(name):
                    moved.append((name, d, "FAIL", rdma_why(name)))
        if "port_xmit_wait" in rec.get("counters", {}):
            d = (rec["counters"]["port_xmit_wait"] or 0) - (pb.get("counters", {}).get("port_xmit_wait") or 0)
            if d > 0:
                moved.append(("port_xmit_wait", d, "INFO", "ticks the port had data but no credits/pause: back-pressure"))
        for name, d, v, why in moved:
            add(sec, f"RDMA {k} {name}", f"+{d}", "0", v, why)
        if not moved and rec:
            add(sec, f"RDMA {k} error/retransmit counters", "0 new", "0", "PASS", f"{len(rec.get('counters', {})) + len(rec.get('hw', {}))} counters checked")
        if pb.get("state") and rec.get("state") != pb.get("state"):
            add(sec, f"RDMA {k} port state", f"{pb.get('state')} -> {rec.get('state')}", "stable", "FAIL")
    # NIC PHY / ethtool
    for nd, st in a.get("ethtool", {}).items():
        pb = b.get("ethtool", {}).get(nd, {})
        moved, corr = [], 0
        for name, v in st.items():
            if BENIGN.search(name) or v is None:
                continue
            d = v - (pb.get(name) or 0)
            if d <= 0:
                continue
            if CORR_PAT.search(name):
                if "bits" in name or "err_lane" in name:
                    corr += d
                moved.append((name, d, "INFO", "corrected by FEC"))
            elif CONG_PAT.search(name):
                moved.append((name, d, "WARN", "pause/ECN/CNP: congestion or a slow receiver (back-to-back: the far end could not drain)"))
            elif ERR_PAT.search(name):
                moved.append((name, d, "FAIL", "PHY/MAC error, drop or discard: lossless RoCE must show zero"))
        rxb = (st.get("rx_bytes_phy") or st.get("rx_bytes") or 0) - (pb.get("rx_bytes_phy") or pb.get("rx_bytes") or 0)
        if corr and rxb > 0:
            ber = corr / (rxb * 8)
            v = "FAIL" if ber > TH["fec_ber_fail"] else ("WARN" if ber > TH["fec_ber_warn"] else "PASS")
            add(sec, f"NIC {nd} pre-FEC bit error rate", f"{ber:.2e}", f"<= {TH['fec_ber_warn']:.0e} (fail > {TH['fec_ber_fail']:.0e})", v,
                f"{corr} corrected bits over {rxb / 1e9:.0f} GB received: a rising BER is a dirty fiber, a bad DAC or a failing optic")
        for name, d, v, why in sorted(moved, key=lambda x: {"FAIL": 0, "WARN": 1, "INFO": 2}[x[2]])[:25]:
            add(sec, f"NIC {nd} {name}", f"+{d}", "0", v, why)
        if not any(m[2] in ("FAIL", "WARN") for m in moved) and st:
            add(sec, f"NIC {nd} PHY/MAC errors, drops, discards, pause", "0 new", "0", "PASS", f"{len(st)} ethtool counters checked")
        mod_a, mod_b = a.get("module", {}).get(nd, {}), b.get("module", {}).get(nd, {})
        if mod_a.get("alarms_on"):
            add(sec, f"NIC {nd} optics alarm flags", "; ".join(mod_a["alarms_on"][:4]), "none", "FAIL")
        if mod_a.get("temp_c") and mod_a["temp_c"] >= TH["nic_module_temp_warn_c"]:
            add(sec, f"NIC {nd} transceiver temperature", f"{mod_a['temp_c']:.0f} C", f"< {TH['nic_module_temp_warn_c']} C", "WARN")
        if mod_a.get("rx_dbm") and mod_b.get("rx_dbm") and min(mod_a["rx_dbm"]) < -8:
            add(sec, f"NIC {nd} optical rx power", f"min {min(mod_a['rx_dbm']):.1f} dBm", ">= -8 dBm", "WARN", "weak received light: clean or replace the fiber/optic")
    # thermal throttling
    for k in ("core", "package"):
        d = (a.get("throttle", {}).get(k) or 0) - (b.get("throttle", {}).get(k) or 0)
        if d > 0:
            add(sec, f"CPU {k} thermal throttle events", f"+{d}", "0", "WARN", "CPU clocked down during the run: cooling/power")
    # dmesg
    old = set(b.get("dmesg", []))
    new = [x for x in a.get("dmesg", []) if x not in old]
    pat = re.compile(r"(error|fail|fatal|AER|Hardware Error|mce|machine check|EDAC|Xid|timeout|timed out|reset|I/O error|blk_update_request|"
                     r"critical|nvme.*(abort|disabl)|mlx5.*(syndrome|health|fw_fatal|cmd_exec)|soft lockup|hung_task|Out of memory|oom-kill|"
                     r"link (is )?down|throttl|PCIe Bus Error|corrected)", re.I)
    hits = [x for x in new if pat.search(x)]
    if hits:
        for x in hits[:12]:
            add(sec, "dmesg (new during the run)", x.strip()[-150:], "no hardware/driver errors",
                "FAIL" if re.search(r"(fatal|uncorrect|I/O error|Hardware Error|oom|hung_task|soft lockup|link (is )?down|abort|reset)", x, re.I) else "WARN")
        if len(hits) > 12:
            add(sec, "dmesg (new during the run)", f"... {len(hits) - 12} more lines", "", "WARN")
    elif a.get("dmesg"):
        add(sec, "dmesg (new during the run)", f"{len(new)} new lines, none hardware/driver errors", "", "PASS")
    else:
        add(sec, "dmesg", "unreadable (not root or dmesg_restrict)", "", "SKIP")
    for x in a.get("mce", [])[:3]:
        if x not in b.get("mce", []):
            add(sec, "machine check (journal)", x.strip()[-150:], "none", "FAIL")
    # peak temps during the run
    if ser:
        mx = {}
        for x in ser.s:
            for k, v in x.get("temp", {}).items():
                mx[k] = max(mx.get(k, -1e9), v)
        nv = {k: v for k, v in mx.items() if k.startswith("nvme")}
        if nv:
            k, v = max(nv.items(), key=lambda kv: kv[1])
            add(sec, "NVMe peak temperature during the run", f"{v:.0f} C ({k.split(':')[1] or k})", f"< {TH['nvme_temp_warn_c']} C",
                "PASS" if v < TH["nvme_temp_warn_c"] else "WARN", f"{len(nv)} NVMe sensors sampled every 5 s")
        ml = {k: v for k, v in mx.items() if k.startswith("mlx5")}
        if ml:
            k, v = max(ml.items(), key=lambda kv: kv[1])
            add(sec, "NIC ASIC peak temperature during the run", f"{v:.0f} C", "< 95 C", "PASS" if v < 95 else "WARN")


def rdma_why(name):
    n = name.lower()
    for k, why in (("symbol", "physical symbol errors: cable/optic/connector"), ("link_downed", "the link went down"),
                   ("link_error_recovery", "link retrained"), ("rcv_errors", "malformed packets received"),
                   ("xmit_discards", "packets discarded on send (port down or congested)"), ("integrity", "local link integrity errors"),
                   ("buffer_overrun", "receive buffer overruns: flow control not working"), ("out_of_buffer", "no receive WQE posted: receiver too slow"),
                   ("out_of_sequence", "out-of-sequence packets: drops on a lossy fabric force go-back-N"), ("packet_seq", "NAK sequence errors: packet loss"),
                   ("ack_timeout", "ACK timeouts: a packet or ACK was lost, the QP retransmitted"), ("rnr_nak", "receiver-not-ready retries exhausted"),
                   ("implied_nak", "implied NAKs: lost packets"), ("duplicate_request", "duplicate requests received: the peer retransmitted"),
                   ("slow_restart", "RoCE slow restart after loss"), ("cqe_error", "work completions with error status"),
                   ("icrc", "ICRC errors: corrupted RoCE payload"), ("retrans", "RoCE retransmissions")):
        if k in n:
            return why
    return "error counter"


# ── rendering ───────────────────────────────────────────────────────────────────────────────
def render(run, meta, info):
    L = []
    c = lambda v: f"{COL.get(v, '')}{v}{chr(27) + '[0m' if COL else ''}"
    L.append("=" * 118)
    L.append(f"MOONCAKE STORE LOAD TEST  {meta.get('host', '?')}  {meta.get('started', '')}  mode {meta.get('mode')}  profile {meta.get('profile')}")
    L.append(f"run dir {run}")
    L.append("=" * 118)
    sec = None
    w = max((len(r["check"]) for r in rows), default=40)
    w = min(w, 62)
    for r in rows:
        if r["section"] != sec:
            sec = r["section"]; L.append(""); L.append(f"── {sec} " + "─" * max(0, 114 - len(sec)))
        chk = r["check"] if len(r["check"]) <= w else r["check"][:w - 1] + "…"
        L.append(f"  {c(r['verdict']):<4}  {chk:<{w}}  {str(r['measured']):>22}  {r['bar']}")
        if r["detail"]:
            L.append(f"        {' ' * 0}{r['detail']}")
    if bottlenecks:
        L.append(""); L.append("── bottlenecks (resource utilization vs its own ceiling, per phase) " + "─" * 50)
        for ph, bn in bottlenecks:
            L.append(f"  {ph:<18} {bn['verdict']}")
            for t in bn["top"]:
                L.append(f"      {t['score']:5.0%}  {t['resource']:<44} {t['evidence']}")
            for f in bn["facts"]:
                L.append(f"             {f}")
    if notes:
        L.append(""); L.append("── findings " + "─" * 106)
        for n in notes:
            L.append(f"  * {n}")
    cnt = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("PASS", "WARN", "FAIL", "SKIP", "INFO")}
    L.append(""); L.append("=" * 118)
    fails = [r for r in rows if r["verdict"] == "FAIL"]
    L.append(f"VERDICT: {c('FAIL') if fails else c('PASS')}   {cnt['PASS']} pass, {cnt['WARN']} warn, {cnt['FAIL']} fail, {cnt['SKIP']} skip"
             + ("" if STRICT else "   (lenient: correctable errors counted as WARN)"))
    for r in fails[:15]:
        L.append(f"  FAIL  [{r['section']}] {r['check']}: {r['measured']} (bar {r['bar']})")
    if len(fails) > 15:
        L.append(f"  ... {len(fails) - 15} more in report.txt")
    L.append("=" * 118)
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
PY

cat >"$LIB/stream.c" <<'C'
// STREAM triad a[i] = b[i] + s*c[i], OpenMP, 1 GiB per array, best of 10. Prints GB/s.
#include <stdio.h>
#include <stdlib.h>
#include <omp.h>
#define N (1ull << 27)
#define REPS 10
int main(void) {
    double *a = aligned_alloc(64, N * sizeof(double)), *b = aligned_alloc(64, N * sizeof(double)), *c = aligned_alloc(64, N * sizeof(double));
    if (!a || !b || !c) { perror("alloc"); return 1; }
    const double s = 3.0;
    #pragma omp parallel for
    for (size_t i = 0; i < N; i++) { a[i] = 1.0; b[i] = 2.0; c[i] = 0.5; }   /* first touch on the bound node */
    double best = 0.0;
    for (int r = 0; r < REPS; r++) {
        double t0 = omp_get_wtime();
        #pragma omp parallel for
        for (size_t i = 0; i < N; i++) a[i] = b[i] + s * c[i];
        double gbps = 3.0 * N * sizeof(double) / (omp_get_wtime() - t0) / 1e9;
        if (gbps > best) best = gbps;
    }
    printf("%.2f\n", best);
    return a[N / 2] == 2.0;
}
C
}

# ════════════════════════════════════════════════════════════════════════════════════════════
# Profiles: how long each phase runs and how big the store is.
# ════════════════════════════════════════════════════════════════════════════════════════════
apply_profile() {
  case "$PROFILE" in
    quick)    FABRIC_S=10; RAW_S=20;  FS_GB=2;  SWEEP_S=10; MIXED_S=20;  NVME_GET_GB=32;  SEG_CAP_GB=16; DISK_MULT=4;  NVME_MAX_S=300 ;;
    standard) FABRIC_S=30; RAW_S=60;  FS_GB=8;  SWEEP_S=20; MIXED_S=60;  NVME_GET_GB=128; SEG_CAP_GB=0;  DISK_MULT=10; NVME_MAX_S=900 ;;
    soak)     FABRIC_S=60; RAW_S=300; FS_GB=32; SWEEP_S=30; MIXED_S=300; NVME_GET_GB=512; SEG_CAP_GB=0;  DISK_MULT=0;  NVME_MAX_S=3600 ;;
    *) die "--profile quick|standard|soak" ;;
  esac
  # env overrides for every knob (MCL_FABRIC_S=... etc.)
  FABRIC_S=${MCL_FABRIC_S:-$FABRIC_S}; RAW_S=${MCL_RAW_S:-$RAW_S}; FS_GB=${MCL_FS_GB:-$FS_GB}; SWEEP_S=${MCL_SWEEP_S:-$SWEEP_S}
  MIXED_S=${MCL_MIXED_S:-$MIXED_S}; NVME_GET_GB=${MCL_NVME_GET_GB:-$NVME_GET_GB}; NVME_MAX_S=${MCL_NVME_MAX_S:-$NVME_MAX_S}
}

# ════════════════════════════════════════════════════════════════════════════════════════════
# Server side: the store box.
# ════════════════════════════════════════════════════════════════════════════════════════════
S=""; SRV_PIDS=()
start_bg() { # name cmd...: start in background, record pid for teardown
  local name=$1; shift
  "$@" & local pid=$!
  echo "$pid $name" >>"$S/pids"; SRV_PIDS+=("$pid")
}
numa_pin() { local n=$1; [ -n "$n" ] && [ "$n" -ge 0 ] 2>/dev/null && command -v numactl >/dev/null && echo "numactl -N $n -m $n" || true; }
lane_field() { "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))[int(sys.argv[2])].get(sys.argv[3]) or '')" "$S/lanes.json" "$1" "$2"; }

lane_count() { "$PY" -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$S/lanes.json"; }

phase_dram() {
  command -v gcc >/dev/null || { warn "dram: no gcc, skipping STREAM"; return; }
  log "dram: STREAM triad per NUMA node, local and remote"
  gcc -O3 -fopenmp -o "$WORK/stream" "$LIB/stream.c" 2>"$S/stream.build.log" || { warn "dram: STREAM build failed"; return; }
  local nodes=() n m t0 v
  mapfile -t nodes < <(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | sed 's/.*node//' | sort -n)
  [ ${#nodes[@]} -gt 0 ] || nodes=(0)
  : >"$S/stream.tsv"
  for n in "${nodes[@]}"; do for m in "${nodes[@]}"; do
    t0=$(now)
    if command -v numactl >/dev/null && [ -d /sys/devices/system/node/node$n ]; then
      local th; th=$(numactl -H | awk -v n="$n" '$1=="node" && $2==n && $3=="cpus:" {print NF-3}')
      v=$(OMP_NUM_THREADS=${th:-1} OMP_PROC_BIND=true numactl -N "$n" -m "$m" "$WORK/stream" 2>/dev/null || echo 0)
    else v=$("$WORK/stream" 2>/dev/null || echo 0); fi
    mark "dram:$n-$m" "$t0" "$(now)" "$S/marks.jsonl"
    echo "$n $m $v" >>"$S/stream.tsv"; log "  cpu node $n -> mem node $m: $v GB/s"
  done; done
}

raw_fio_job() {
  local eng=$1 bs=$2 rw=$3 rt=$4; shift 4
  case "$rw" in read|randread) ;; *) die "raw fio permits read workloads only" ;; esac
  printf '[global]\nioengine=%s\ndirect=1\niodepth=32\nbs=%s\nrw=%s\ntime_based=1\nruntime=%s\nramp_time=2\nnorandommap=1\nrandrepeat=0\n' "$eng" "$bs" "$rw" "$rt"
  local path
  for path in "$@"; do printf '[%s]\nfilename=%s\n' "${path##*/}" "$path"; done
}

fs_fio_job() {
  local tag=$1 dir=$2 eng=$3 per_job=$4 bytes=$5 seconds=$6
  case $tag in
    bufwrite) printf '[global]\ndirectory=%s\nioengine=psync\ndirect=0\nrw=write\nbs=1M\nnumjobs=8\nfilesize=1G\nsize=%sM\nend_fsync=1\nfsync_on_close=1\nfilename_format=b.$jobnum.$filenum\ngroup_reporting=1\n[w]\n' "$dir" "$per_job" ;;
    dirwrite) printf '[global]\ndirectory=%s\nioengine=%s\ndirect=1\nrw=write\nbs=1M\niodepth=32\nnumjobs=8\nfilesize=1G\nsize=%sM\nfilename_format=d.$jobnum.$filenum\ngroup_reporting=1\n[w]\n' "$dir" "$eng" "$per_job" ;;
    objread) printf '[global]\ndirectory=%s\nioengine=%s\ndirect=1\nrw=randread\nbs=%s\nblockalign=4k\niodepth=32\nnumjobs=8\nfilesize=1G\nsize=%sM\nfilename_format=b.$jobnum.$filenum\ntime_based=1\nruntime=%s\nnorandommap=1\nrandrepeat=0\ngroup_reporting=1\n[r]\n' "$dir" "$eng" "$bytes" "$per_job" "$seconds" ;;
    *) die "unknown filesystem benchmark: $tag" ;;
  esac
}

phase_nvme_raw() {   # read-only: safe on drives that carry data and mounted filesystems
  local ns=() d
  for d in /sys/block/nvme*n*; do [[ ${d##*/} =~ ^nvme[0-9]+n[0-9]+$ ]] && ns+=("${d##*/}"); done
  [ ${#ns[@]} -gt 0 ] || { log "nvme: no NVMe namespaces"; return; }
  command -v fio >/dev/null || { warn "nvme: fio missing"; return; }
  local eng=io_uring; fio --enghelp=io_uring >/dev/null 2>&1 || eng=libaio
  for tag in seqread objread; do
    local bs=1M rw=read rt=$RAW_S
    [ "$tag" = objread ] && { bs=$OBJ_ALIGNED; rw=randread; rt=$(( (RAW_S + 1) / 2 )); }
    local paths=(); for d in "${ns[@]}"; do paths+=("/dev/$d"); done
    raw_fio_job "$eng" "$bs" "$rw" "$rt" "${paths[@]}" >"$S/nvme/raw-$tag.fio"
    log "nvme: raw $tag on ${#ns[@]} drives at once (read-only, ${rt}s, bs $bs, QD32)"
    local t0; t0=$(now)
    fio --readonly --output-format=json --output="$S/nvme/raw-$tag.json" "$S/nvme/raw-$tag.fio" >/dev/null 2>"$S/nvme/raw-$tag.err" || warn "raw $tag fio failed (see nvme/raw-$tag.err)"
    mark "nvme:raw-$tag" "$t0" "$(now)" "$S/marks.jsonl"
    "$PY" -c 'import json,sys; j=json.load(open(sys.argv[1])); print("   ", "  ".join(f"{x[\"jobname\"]} {x[\"read\"][\"bw_bytes\"]/1e9:.2f}" for x in j["jobs"]), f"| total {sum(x[\"read\"][\"bw_bytes\"] for x in j[\"jobs\"])/1e9:.1f} GB/s")' "$S/nvme/raw-$tag.json" 2>/dev/null >&2 || true
  done
}

phase_fs() {   # ceilings of the exact L3 directories the legs will use
  command -v fio >/dev/null || { warn "fs: fio missing"; return; }
  local n i dir eng=io_uring; fio --enghelp=io_uring >/dev/null 2>&1 || eng=libaio
  n=$(lane_count)
  local per_job=$(( FS_GB * 1024 / 8 ))   # MiB per job, 8 jobs
  for tag in bufwrite dirwrite objread; do
    local t0; t0=$(now); local pids=()
    for i in $(seq 0 $((n - 1))); do
      dir=$(lane_field "$i" disk_dir)/fio; mkdir -p "$dir"
      fs_fio_job "$tag" "$dir" "$eng" "$per_job" "$OBJ_ALIGNED" "$RAW_S" >"$S/nvme/fs-$tag-$i.fio"
      fio --output-format=json --output="$S/nvme/fs-$tag-$i.json" "$S/nvme/fs-$tag-$i.fio" >/dev/null 2>"$S/nvme/fs-$tag-$i.err" &
      pids+=($!)
    done
    for p in "${pids[@]}"; do wait "$p" || warn "fs $tag fio failed (see nvme/fs-$tag-*.err)"; done
    mark "fs:$tag" "$t0" "$(now)" "$S/marks.jsonl"
    for i in $(seq 0 $((n - 1))); do
      "$PY" -c 'import json,sys; j=json.load(open(sys.argv[1])); s=sys.argv[3]; print(f"    lane {sys.argv[2]} {sys.argv[4]}: {sum(x[s][\"bw_bytes\"] for x in j[\"jobs\"])/1e9:.2f} GB/s")' \
        "$S/nvme/fs-$tag-$i.json" "$i" "$([ $tag = objread ] && echo read || echo write)" "$tag" 2>/dev/null >&2 || true
    done
  done
  for i in $(seq 0 $((n - 1))); do rm -rf "$(lane_field "$i" disk_dir)/fio"; done
}

start_store() {
  local n i mem seg cap arena dev ip node dir mport
  n=$(lane_count)
  mem=$(awk '/MemTotal/ {printf "%d", $2/2^20}' /proc/meminfo)   # GiB
  seg=${SEGMENT_GB:-$(( mem * 30 / 100 / n ))}
  [ -z "$SEGMENT_GB" ] && [ "$SEG_CAP_GB" -gt 0 ] && [ "$seg" -gt "$SEG_CAP_GB" ] && seg=$SEG_CAP_GB
  [ "$seg" -ge 1 ] || seg=1
  arena=$(( mem * 5 / 100 / n )); [ "$arena" -gt 16 ] && arena=16; [ "$arena" -lt 1 ] && arena=1
  mkdir -p "$S/store"
  "$PY" - "$S/lanes.json" "$seg" "$arena" "${DISK_CAP_GB:-}" "$DISK_MULT" <<'PY'
import json, sys
lanes = json.load(open(sys.argv[1])); seg, arena = int(sys.argv[2]), int(sys.argv[3])
cap_flag, mult = sys.argv[4], int(sys.argv[5])
for l in lanes:
    # Reserve 20% of available bytes plus 4 GiB per lane for buckets/fio/logs.
    usable = int(l["disk_avail"] / l["disk_shared_by"] * 0.80) - (4 << 30)
    if usable < 1 << 30:
        raise SystemExit("insufficient scratch disk headroom")
    cap = int(cap_flag) << 30 if cap_flag else (min(usable, mult * (seg << 30)) if mult else usable)
    cap = min(cap, usable) // (1 << 30) * (1 << 30)
    l.update(segment_bytes=seg << 30, arena_bytes=arena << 30, disk_cap_bytes=max(cap, 1 << 30))
json.dump(lanes, open(sys.argv[1], "w"), indent=1)
PY
  for i in $(seq 0 $((n - 1))); do
    dev=$(lane_field "$i" dev); ip=$(lane_field "$i" ip); node=$(lane_field "$i" numa); dir=$(lane_field "$i" disk_dir)
    cap=$(lane_field "$i" disk_cap_bytes); mport=$(port "$i" 0)
    rm -rf "$dir/kv"; mkdir -p "$dir/kv"
    log "lane $i: master :$mport + leg on ${dev:-tcp} $ip numa $node, segment ${seg} GiB, L3 cap $((cap >> 30)) GiB at $dir/kv, arena ${arena} GiB"
    # shellcheck disable=SC2046
    start_bg "master$i" $(numa_pin "$node") "$MC_DIR/mooncake_master" --rpc_port="$mport" --rpc_address="$ip" --metrics_port="$(port "$i" 1)" \
      --enable_offload=true --eviction_high_watermark_ratio=0.80 --eviction_ratio=0.10 >"$S/store/master-$i.log" 2>&1
    cat >"$S/store/leg-$i.json" <<J
{"mode": "standalone-store", "local_hostname": "$ip", "metadata_server": "P2PHANDSHAKE",
 "master_server_address": "$ip:$mport", "global_segment_size": "${seg}GB", "local_buffer_size": "1GB",
 "protocol": "$PROTO", "device_name": "$dev", "enable_ssd_offload": true, "ssd_offload_path": "$dir/kv",
 "enable_client_http_server": true, "client_http_port": $(port "$i" 3)}
J
    # The leg: DRAM segment in front, 1 GiB bucket files behind, LRU bucket eviction between the 0.95/0.85 disk watermarks.
    # shellcheck disable=SC2046
    start_bg "leg$i" env MOONCAKE_OFFLOAD_FILE_STORAGE_PATH="$dir/kv" MOONCAKE_OFFLOAD_LOCAL_BUFFER_SIZE_BYTES=$((arena << 30)) \
      MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES="$cap" MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE="$cap" MOONCAKE_OFFLOAD_BUCKET_MAX_PHYSICAL_BYTES="$cap" \
      MOONCAKE_OFFLOAD_BUCKET_SIZE_LIMIT_BYTES=$((1 << 30)) MOONCAKE_OFFLOAD_BUCKET_EVICTION_POLICY=lru \
      MOONCAKE_OFFLOAD_ENABLE_DISK_WATERMARK_EVICTION=true MOONCAKE_OFFLOAD_DISK_EVICTION_HIGH_WATERMARK_RATIO=0.95 \
      MOONCAKE_OFFLOAD_DISK_EVICTION_LOW_WATERMARK_RATIO=0.85 MOONCAKE_OFFLOAD_USE_URING=true MC_FORCE_HCA=1 MC_MS_AUTO_DISC=0 GLOG_logtostderr=1 \
      $(numa_pin "$node") "$PY" -m mooncake.mooncake_store_service --config "$S/store/leg-$i.json" --port="$(port "$i" 4)" --max-wait-time 300 \
      >"$S/store/leg-$i.log" 2>&1
  done
  for i in $(seq 0 $((n - 1))); do
    local ok=""
    for _ in $(seq 1 120); do
      if "$PY" -c 'import socket,sys; [socket.create_connection((sys.argv[1], int(p)), 1).close() for p in sys.argv[2:]]' "$(lane_field "$i" ip)" "$(port "$i" 0)" "$(port "$i" 3)" 2>/dev/null; then ok=1; break; fi
      grep -qE "Store startup failed: exceeded|Traceback" "$S/store/leg-$i.log" 2>/dev/null && break
      sleep 2
    done
    [ -n "$ok" ] || { tail -30 "$S/store/leg-$i.log" >&2; tail -10 "$S/store/master-$i.log" >&2; die "lane $i: store leg did not come up (logs above)"; }
  done
  sleep 3
  log "store: $n master(s) + leg(s) up"
}

start_fabric_targets() {
  local n i dev ip node use_vram=""
  n=$(lane_count)
  "$MC_DIR/transfer_engine_bench" --help 2>&1 | grep -q 'use_vram' && use_vram="--use_vram=false"
  for i in $(seq 0 $((n - 1))); do
    dev=$(lane_field "$i" dev); ip=$(lane_field "$i" ip); node=$(lane_field "$i" numa)
    # shellcheck disable=SC2046,SC2086
    start_bg "te$i" $(numa_pin "$node") "$MC_DIR/transfer_engine_bench" --mode=target --metadata_server=P2PHANDSHAKE --protocol="$PROTO" \
      ${dev:+--device_name="$dev"} --local_server_name="$ip:$(port "$i" 5)" --buffer_size=$((1 << 30)) --auto_discovery=false $use_vram \
      >"$S/fabric-target-$i.log" 2>&1
    # P2P handshake: the segment name is ip:<rpc port the target picked>, not local_server_name's port
    local seg=""
    for _ in $(seq 1 30); do
      seg=$(grep -oE 'P2P handshake, listening on [^ ]+' "$S/fabric-target-$i.log" 2>/dev/null | awk '{print $NF}' | tail -1 || true)
      [ -n "$seg" ] && break; sleep 1
    done
    [ -n "$seg" ] || { tail -20 "$S/fabric-target-$i.log" >&2; die "lane $i: transfer_engine_bench target did not start"; }
    "$PY" -c 'import json,sys; L=json.load(open(sys.argv[1])); L[int(sys.argv[2])]["te_segment"]=sys.argv[3]; json.dump(L, open(sys.argv[1],"w"), indent=1)' "$S/lanes.json" "$i" "$seg"
    if [ "$PROTO" = rdma ] && command -v ib_write_bw >/dev/null; then
      local gid; gid=$(roce_gid "$dev" "$ip")
      for t in write read; do
        local off=6; [ $t = read ] && off=7
        start_bg "perftest-$t$i" bash -c "while [ ! -f '$S/sampler.stop' ]; do $(numa_pin "$node") ib_${t}_bw -d $dev -p $(port "$i" $off) -F --report_gbits ${gid:+-x $gid} >/dev/null 2>&1 || sleep 1; done"
      done
    fi
  done
}

roce_gid() { # RoCE v2 GID index carrying this IPv4 (perftest otherwise picks GID 0 = RoCE v1)
  local dev=$1 ip=$2 g
  for g in /sys/class/infiniband/"$dev"/ports/1/gids/*; do
    local i=${g##*/}
    [ "$(cat "/sys/class/infiniband/$dev/ports/1/gid_attrs/types/$i" 2>/dev/null)" = "RoCE v2" ] || continue
    printf '%s' "$(cat "$g")" | grep -qi "ffff:$(printf '%02x%02x:%02x%02x' ${ip//./ })" && { echo "$i"; return; }
  done
}

server_setup() { # $1 = run dir
  RUN=$1; S=$RUN/server; mkdir -p "$S/nvme" "$S/store"; : >"$S/pids"; : >"$S/marks.jsonl"
  ulimit -l unlimited 2>/dev/null || warn "could not raise memlock limit (RDMA registration may fail)"
  log "server: discovering lanes (protocol $PROTOCOL${NICS:+, nics $NICS})"
  "$PY" "$LIB/probe.py" lanes --protocol "$PROTOCOL" --nics "$NICS" >"$S/lanes.json"
  grep -q '"error"' "$S/lanes.json" && die "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["error"])' "$S/lanes.json")"
  PROTO=$(lane_field 0 protocol)
  [ "$PROTO" = tcp ] && warn "running TCP diagnostics: explicit TCP is WARN; automatic RDMA fallback is FAIL. This does not qualify the RDMA fast path."
  log "server: inventory"
  "$PY" "$LIB/probe.py" inventory "$S/inventory.json" >&2
  "$PY" "$LIB/probe.py" snapshot "$S/before.json" --netdevs "$(lanes_netdevs)"
  start_bg sampler "$PY" "$LIB/probe.py" sample "$S/samples.jsonl" --stop "$S/sampler.stop" --interval "$SAMPLE_S"
  "$PY" "$LIB/probe.py" diskdirs --lanes "$S/lanes.json" --dirs "$DISK_DIRS" --root "$WORK" >"$S/diskdirs.json"
  "$PY" - "$S/lanes.json" "$S/diskdirs.json" <<'PY'
import json, sys
lanes, dd = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
for l, d in zip(lanes, dd):
    l.update(disk_dir=d["dir"], disk_source=d["source"], disk_fstype=d["fstype"], disk_nvme=d["nvme"], disk_avail=d["avail"], disk_shared_by=d["shared_by"])
    nv = d["nvme"][0] if d["nvme"] else None
    try:
        l["disk_numa"] = int(open(f"/sys/class/nvme/{nv.rsplit('n', 1)[0]}/device/numa_node").read()) if nv else None
    except OSError:
        l["disk_numa"] = None
json.dump(lanes, open(sys.argv[1], "w"), indent=1)
for l in lanes:
    print(f"  lane {l['idx']}: {l['dev'] or l['netdev']} {l['ip']} numa {l['numa']} {l['rate_gbit']} Gb/s -> L3 {l['disk_dir']} ({l['disk_source']} {l['disk_fstype']}, "
          f"{','.join(l['disk_nvme']) or 'NOT NVMe'}, {l['disk_avail'] / 2**30:.0f} GiB free, shared by {l['disk_shared_by']})", file=sys.stderr)
PY
  [[ ",$SKIP," == *",dram,"* ]] || phase_dram
  [[ ",$SKIP," == *",nvme,"* ]] || phase_nvme_raw
  [[ ",$SKIP," == *",fs,"*   ]] || phase_fs
  [[ ",$SKIP," == *",store,"* ]] || start_store
  [[ ",$SKIP," == *",fabric,"* ]] || start_fabric_targets
  "$PY" - "$S/lanes.json" "$S/info.json" "$PROTO" "$OBJECT_BYTES" "$PORT_BASE" "$(wheel_version)" "$PROFILE" <<'PY'
import json, socket, sys, time
lanes = json.load(open(sys.argv[1]))
base = int(sys.argv[5])
for l in lanes:
    i = l["idx"]
    l.update(master=f"{l['ip']}:{base + i * 20}", metrics_port=base + i * 20 + 1, te_port=base + i * 20 + 5,
             perftest_write_port=base + i * 20 + 6, perftest_read_port=base + i * 20 + 7)
json.dump({"host": socket.gethostname(), "protocol": sys.argv[3], "object_bytes": int(sys.argv[4]), "lanes": lanes,
           "mooncake": sys.argv[6], "profile": sys.argv[7], "ready_at": time.time()}, open(sys.argv[2], "w"), indent=1)
PY
}
lanes_netdevs() { "$PY" -c "import json,sys; print(','.join(l.get('netdev','') for l in json.load(open(sys.argv[1]))))" "$S/lanes.json"; }

server_cleanup() {
  [ -n "$S" ] && [ -f "$S/pids" ] || return 0
  touch "$S/sampler.stop" 2>/dev/null || true
  local p; for p in "${SRV_PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  # Only PIDs launched by this shell; never match other stores by process name.
  local until=$((SECONDS + 5)) alive
  while [ "$SECONDS" -lt "$until" ]; do
    alive=""
    for p in "${SRV_PIDS[@]:-}"; do [ -n "$p" ] && kill -0 "$p" 2>/dev/null && alive=1 || true; done
    [ -n "$alive" ] || break
    sleep 0.1
  done
  for p in "${SRV_PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null || true; done
}

# ════════════════════════════════════════════════════════════════════════════════════════════
# Client side: the engine stand-in.
# ════════════════════════════════════════════════════════════════════════════════════════════
fetch() { "$PY" -c 'import sys,urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=10).read().decode())' "$1"; }

client_lanes() { # map server lanes to local devices by index; writes $C/lanes.json (with server fields)
  if [ "$MODE" = single ]; then
    "$PY" -c 'import json,sys; L=json.load(open(sys.argv[1]))["lanes"]; [l.update(local_ip=l["ip"], local_dev=l["dev"]) for l in L]; json.dump(L, open(sys.argv[2],"w"), indent=1)' "$C/info.json" "$C/lanes.json"
    return
  fi
  "$PY" "$LIB/probe.py" lanes --protocol "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol"])' "$C/info.json")" --nics "$NICS" >"$C/local-lanes.json"
  grep -q '"error"' "$C/local-lanes.json" && die "client: $(cat "$C/local-lanes.json")"
  "$PY" - "$C/info.json" "$C/local-lanes.json" "$C/lanes.json" <<'PY'
import json, sys
srv, loc = json.load(open(sys.argv[1]))["lanes"], json.load(open(sys.argv[2]))
out = []
for i, l in enumerate(srv):
    m = loc[i % len(loc)]
    out.append(dict(l, local_ip=m["ip"], local_dev=m["dev"], local_netdev=m["netdev"], local_numa=m["numa"], local_rate_gbit=m["rate_gbit"]))
    print(f"  lane {i}: client {m['dev'] or m['netdev']} {m['ip']} -> server {l['dev'] or l['netdev']} {l['ip']}", file=sys.stderr)
json.dump(out, open(sys.argv[3], "w"), indent=1)
PY
}
cl_field() { "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))[int(sys.argv[2])].get(sys.argv[3]) or '')" "$C/lanes.json" "$1" "$2"; }

fabric_one() { # lane op tag
  local i=$1 op=$2 tag=$3 dev ip sip node use_vram=""
  dev=$(cl_field "$i" local_dev); ip=$(cl_field "$i" local_ip); sip=$(cl_field "$i" ip); node=$(cl_field "$i" local_numa)
  [ "$MODE" = single ] && node=$(cl_field "$i" numa)
  "$MC_DIR/transfer_engine_bench" --help 2>&1 | grep -q 'use_vram' && use_vram="--use_vram=false"
  local t0; t0=$(now)
  # shellcheck disable=SC2046,SC2086
  $(numa_pin "$node") "$MC_DIR/transfer_engine_bench" --mode=initiator --metadata_server=P2PHANDSHAKE --protocol="$PROTO" ${dev:+--device_name="$dev"} \
    --local_server_name="$ip:$(port "$i" 8)" --segment_id="$(cl_field "$i" te_segment)" --operation="$op" --block_size=65536 --batch_size=128 \
    --threads=16 --duration="$FABRIC_S" --buffer_size=$((1 << 30)) --auto_discovery=false --report_unit=Gb $use_vram >"$C/fabric-$tag.log" 2>&1 \
    || warn "fabric $tag: transfer_engine_bench failed (see client/fabric-$tag.log)"
  mark "fabric:$tag" "$t0" "$(now)" "$C/marks.jsonl"
  local g; g=$(grep -oE 'throughput[^0-9]*[0-9.]+' "$C/fabric-$tag.log" | grep -oE '[0-9.]+$' | tail -1 || true)
  echo "$tag ${g:-null}" >>"$C/fabric.tsv"; log "  $tag: ${g:-?} Gb/s"
}

perftest_one() { # lane op
  local i=$1 op=$2 dev sip gid off=6 node
  dev=$(cl_field "$i" local_dev); sip=$(cl_field "$i" ip); node=$(cl_field "$i" local_numa)
  [ "$MODE" = single ] && node=$(cl_field "$i" numa)
  [ "$op" = read ] && off=7
  gid=$(roce_gid "$dev" "$(cl_field "$i" local_ip)")
  local t0; t0=$(now)
  # shellcheck disable=SC2046
  timeout $((FABRIC_S + 30)) $(numa_pin "$node") "ib_${op}_bw" -d "$dev" -p "$(port "$i" $off)" -F --report_gbits -s 65536 -q 8 -D "$FABRIC_S" ${gid:+-x "$gid"} "$sip" \
    >"$C/fabric-perftest-$op-$i.log" 2>&1 || warn "perftest $op lane $i failed (see client/fabric-perftest-$op-$i.log)"
  mark "fabric:perftest-$op-$i" "$t0" "$(now)" "$C/marks.jsonl"
  local g; g=$(awk '/^ *[0-9]+ +[0-9]+ +[0-9.]+ +[0-9.]+/ {v=$4} END {print v}' "$C/fabric-perftest-$op-$i.log" || true)
  echo "perftest-$op-$i ${g:-null}" >>"$C/fabric.tsv"; log "  perftest-$op-$i: ${g:-?} Gb/s"
}

phase_fabric() {
  local n i op; n=$("$PY" -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$C/lanes.json")
  : >"$C/fabric.tsv"
  log "fabric: Mooncake Transfer Engine, 64 KiB slices, 16 threads, ${FABRIC_S}s per run"
  for i in $(seq 0 $((n - 1))); do for op in write read; do fabric_one "$i" "$op" "te-$op-$i"; done; done
  if [ "$n" -gt 1 ]; then
    for op in write read; do
      log "fabric: all $n lanes at once, $op"
      for i in $(seq 0 $((n - 1))); do fabric_one "$i" "$op" "te-$op-all-$i" & done; wait
    done
  fi
  if [ "$PROTO" = rdma ] && command -v ib_write_bw >/dev/null; then
    log "fabric: perftest raw-verbs reference"
    for i in $(seq 0 $((n - 1))); do perftest_one "$i" write; perftest_one "$i" read; done
  fi
}

phase_store_bench() {
  local W=${WORKERS:-16}
  "$PY" - "$C/lanes.json" "$C/bench-config.json" <<PY
import json, sys, time
lanes = json.load(open(sys.argv[1]))
cfg = {"lanes": [{"idx": l["idx"], "master": l["master"], "local_ip": l["local_ip"], "local_dev": l["local_dev"], "local_numa": l.get("local_numa", l.get("numa", -1)),
                  "segment_bytes": l["segment_bytes"], "disk_cap_bytes": l["disk_cap_bytes"]} for l in lanes],
       "object_bytes": $OBJECT_BYTES, "workers": $W, "batch": $BATCH, "protocol": "$PROTO", "fill_ratio": $FILL_RATIO,
       "sweep_s": $SWEEP_S, "mixed_s": $MIXED_S, "nvme_get_bytes": $NVME_GET_GB << 30, "nvme_get_max_s": $NVME_MAX_S,
       "local_buffer": 64 << 20, "seed": 0xC0FFEE, "run_id": "r" + hex(time.time_ns())[2:],
       "phase_timeout_s": max(900, $NVME_MAX_S + 120), "out": "$C/bench.json", "marks": "$C/marks.jsonl"}
json.dump(cfg, open(sys.argv[2], "w"), indent=1)
PY
  log "store: load generator, $W workers x batch $BATCH per lane, object $OBJECT_BYTES B"
  ulimit -l unlimited 2>/dev/null || true
  MC_MS_AUTO_DISC=0 GLOG_logtostderr=1 GLOG_minloglevel=1 timeout "$((NVME_MAX_S + SWEEP_S * 8 + MIXED_S + 1800))" "$PY" "$LIB/bench.py" --config "$C/bench-config.json" 2>&1 | tee "$C/bench.log" | grep -vE '^[IWE][0-9]{4} ' >&2 \
    || warn "store bench exited nonzero (see client/bench.log)"
}

client_run() { # $1 = server control url
  local url=$1
  C=$RUN/client; mkdir -p "$C"; : >"$C/marks.jsonl"
  if [ "$MODE" != single ]; then
    log "client: waiting for the server at $url (it runs its local phases first)"
    local t_end=$(( $(date +%s) + 6 * 3600 ))
    until fetch "$url/info" >"$C/info.json" 2>/dev/null; do [ "$(date +%s)" -lt "$t_end" ] || die "server never answered at $url"; sleep 5; done
    "$PY" - "$url" "$RUN/meta.json" <<'PY'
import json, sys, time, urllib.request
best = None
for _ in range(9):
    t0 = time.time(); ts = json.loads(urllib.request.urlopen(sys.argv[1] + "/time", timeout=5).read())["t"]; t1 = time.time()
    if best is None or t1 - t0 < best[0]:
        best = (t1 - t0, ts - (t0 + t1) / 2)
m = json.load(open(sys.argv[2])); m["clock_offset"] = best[1]; m["clock_rtt"] = best[0]
json.dump(m, open(sys.argv[2], "w"), indent=1)
PY
    ulimit -l unlimited 2>/dev/null || true
    "$PY" "$LIB/probe.py" inventory "$C/inventory.json" >&2
    "$PY" "$LIB/probe.py" snapshot "$C/before.json"
    "$PY" "$LIB/probe.py" sample "$C/samples.jsonl" --stop "$C/sampler.stop" --interval "$SAMPLE_S" & CL_SAMPLER=$!
  else
    cp "$S/info.json" "$C/info.json"
  fi
  PROTO=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["protocol"])' "$C/info.json")
  OBJECT_BYTES=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["object_bytes"])' "$C/info.json")   # the server measured its ceilings at this size
  client_lanes
  [[ ",$SKIP," == *",fabric,"* ]] || phase_fabric
  [[ ",$SKIP," == *",store,"* ]] || phase_store_bench
  if [ "$MODE" != single ]; then
    log "client: asking the server to tear down and send its data"
    "$PY" - "$url" "$C/marks.jsonl" "$RUN" <<'PY'
import io, sys, tarfile, urllib.request
body = open(sys.argv[2], "rb").read()
r = urllib.request.urlopen(urllib.request.Request(sys.argv[1] + "/finish", data=body, method="POST"), timeout=600)
with tarfile.open(fileobj=io.BytesIO(r.read()), mode="r:gz") as archive:
    # Remote control server is untrusted: never extract links or special files.
    from pathlib import Path
    root = Path(sys.argv[3]).resolve()
    for member in archive.getmembers():
        target = (root / member.name).resolve()
        if not target.is_relative_to(root) or not (member.isfile() or member.isdir()):
            raise RuntimeError("unsafe report archive member")
    archive.extractall(root)
PY
    "$PY" "$LIB/probe.py" snapshot "$C/after.json" --netdevs "$("$PY" -c "import json,sys; print(','.join(l.get('local_netdev','') or '' for l in json.load(open(sys.argv[1]))))" "$C/lanes.json")"
    touch "$C/sampler.stop"; wait "$CL_SAMPLER" 2>/dev/null || true
    "$PY" -c 'import json,sys; i=json.load(open(sys.argv[1])); i["client_lanes"]=[dict(l, dev=l["local_dev"], ip=l["local_ip"], netdev=l.get("local_netdev"), numa=l.get("local_numa"), rate_gbit=l.get("local_rate_gbit")) for l in json.load(open(sys.argv[2]))]; json.dump(i, open(sys.argv[1],"w"), indent=1)' "$RUN/server/info.json" "$C/lanes.json"
  fi
}

# ════════════════════════════════════════════════════════════════════════════════════════════
# Modes
# ════════════════════════════════════════════════════════════════════════════════════════════
new_run() {
  RUN=${OUT:-$WORK/runs/$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)-$MODE}
  mkdir -p "$RUN"
  "$PY" - "$RUN/meta.json" "$MODE" "$PROFILE" "$PROTOCOL" "$MCL_VERSION" "$*" <<'PY'
import json, socket, sys, time
json.dump({"mode": sys.argv[2], "profile": sys.argv[3], "protocol_requested": sys.argv[4], "script": sys.argv[5], "argv": sys.argv[6],
           "host": socket.gethostname(), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "clock_offset": 0.0},
          open(sys.argv[1], "w"), indent=1)
PY
  log "run directory $RUN"
}

finish_report() {
  local rc=0
  MCL_LENIENT=$LENIENT "$PY" "$LIB/report.py" "$RUN" || rc=$?
  tar -C "$(dirname "$RUN")" -czf "$RUN.tar.gz" "$(basename "$RUN")" 2>/dev/null || true
  echo
  log "report: $RUN/report.txt   data: $RUN.tar.gz"
  return $rc
}

mode_single() {
  install_all; apply_profile; new_run "$@"
  trap 'server_cleanup' EXIT
  trap 'server_cleanup; exit 130' INT TERM
  server_setup "$RUN"
  client_run ""
  log "teardown: after-snapshot, metrics, logs"
  "$PY" "$LIB/ctl.py" finish "$S"
  finish_report
}

mode_server() {
  install_all; apply_profile; new_run "$@"
  trap 'server_cleanup' EXIT
  trap 'server_cleanup; exit 130' INT TERM
  server_setup "$RUN"
  local ips; ips=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | head -3 | tr '\n' ' ' || true)
  log "server READY. On the client run:"
  echo "    curl -fsSL $MCL_URL | sudo bash -s -- client --server <one of: ${ips}> [--profile $PROFILE]" >&2
  log "control endpoint :$CTL_PORT (blocks until the client finishes; Ctrl-C aborts)"
  "$PY" "$LIB/ctl.py" serve "$S" "$CTL_PORT"
  log "client collected the results; server copy at $S"
}

mode_client() {
  [ -n "$SERVER" ] || die "client needs --server <server address>"
  install_all; apply_profile; new_run "$@"
  local url="http://${SERVER}"; [[ "$SERVER" == *:* ]] || url="$url:$CTL_PORT"
  client_run "$url"
  finish_report
}

mode_clean() {
  die "use the run directory's server/pids to inspect owned processes; global process killing is excluded"
}

main() {
  MODE=single
  case "${1:-}" in single|server|client|report|clean) MODE=$1; shift ;; -h|--help|help) usage ;; esac
  PROFILE=quick PROTOCOL=auto NICS="" DISK_DIRS="" SEGMENT_GB="" DISK_CAP_GB="" OBJECT_BYTES=21233664
  WORKERS="" BATCH=4 SERVER="" CTL_PORT=$CTL_PORT_DEFAULT SKIP="" WHEEL=non-cuda MC_VERSION="" NO_INSTALL="" OUT="" FILL_RATIO=1.1
  LENIENT="" SAMPLE_S=1 REPORT_DIR=""
  [ "$MODE" = report ] && { REPORT_DIR=${1:?report <run-dir>}; shift; }
  local argv="$*"
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile) PROFILE=$2; shift 2 ;;            --protocol) PROTOCOL=$2; shift 2 ;;
      --nics) NICS=$2; shift 2 ;;                  --disk-dirs) DISK_DIRS=$2; shift 2 ;;
      --segment-gb) SEGMENT_GB=$2; shift 2 ;;
      --disk-cap-gb) DISK_CAP_GB=$2; shift 2 ;;    --object-bytes) OBJECT_BYTES=$2; shift 2 ;;
      --workers) WORKERS=$2; shift 2 ;;            --batch) BATCH=$2; shift 2 ;;
      --server) SERVER=$2; shift 2 ;;              --ctl-port) CTL_PORT=$2; shift 2 ;;
      --skip) SKIP=$2; shift 2 ;;                  --wheel) WHEEL=$2; shift 2 ;;
      --mooncake-version) MC_VERSION=$2; shift 2 ;; --no-install) NO_INSTALL=1; shift ;;
      --out) OUT=$2; shift 2 ;;                    --fill-ratio) FILL_RATIO=$2; shift 2 ;;
      --lenient) LENIENT=1; shift ;;               --sample-s) SAMPLE_S=$2; shift 2 ;;
      -h|--help) usage ;;
      *) echo "unknown flag: $1" >&2; usage ;;
    esac
  done
  case "$PROTOCOL" in auto|rdma|tcp) ;; *) die "--protocol auto|rdma|tcp" ;; esac
  OBJ_ALIGNED=$(( (OBJECT_BYTES + 4095) / 4096 * 4096 ))
  [ "$PROFILE" = soak ] && [ "$SAMPLE_S" = 1 ] && SAMPLE_S=2
  [ "$(uname -s)" = Linux ] || die "Linux only"
  if [ "$MODE" = report ]; then
    [ -x "$PY" ] || die "no $PY: run a test first"
    MCL_LENIENT=$LENIENT "$PY" "$LIB/report.py" "$REPORT_DIR"; exit $?
  fi
  [ "$(id -u)" = 0 ] || die "run as root (sudo): installs packages, pins memory for RDMA, reads SMART/AER/EDAC counters"
  log "mooncake-loadtest $MCL_VERSION: mode $MODE, profile $PROFILE"
  case "$MODE" in
    single) mode_single "$argv" ;;
    server) mode_server "$argv" ;;
    client) mode_client "$argv" ;;
    clean)  mode_clean ;;
  esac
}

# Everything above is definitions: bash has read the whole script (also when piped through `curl | bash`)
# before main runs, and no child can swallow the rest of it from stdin.
if [[ "${BASH_SOURCE[0]:-$0}" = "$0" ]]; then main "$@" </dev/null; fi
