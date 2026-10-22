#!/usr/bin/env python3
"""Run one benchmark with verified, temporary Linux/NUMA settings and evidence."""

import argparse
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import sys
import threading
import time

PAGES_PER_NODE = 16384  # 32 GiB of 2 MiB HugeTLB pages on each of two nodes.
ENVIRONMENT = {
    "MC_STORE_USE_HUGEPAGE": "1",
    "MC_STORE_HUGEPAGE_SIZE": "2MB",
    "MC_STORE_MEMCPY": "1",
    "MC_TE_METRIC": "1",
    "MC_TE_METRIC_INTERVAL_SECONDS": "5",
}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def no_fio(proc=Path("/proc")):
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text().strip()
            executable = (entry / "exe").readlink().name.removesuffix(" (deleted)")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if name == "fio" or executable == "fio":
            raise RuntimeError(f"fio is still running (PID {entry.name})")


def hugepages(nodes):
    return {
        node.name: {
            key: int((node / "hugepages/hugepages-2048kB" / key).read_text())
            for key in ("nr_hugepages", "free_hugepages", "surplus_hugepages")
        }
        for node in nodes
    }


def at_least(value, minimum):
    return value if value == resource.RLIM_INFINITY else max(value, minimum)


class Profile:
    def __init__(self, sys_root=Path("/sys"), proc=Path("/proc")):
        self.proc = proc
        self.nodes = sorted(
            p for p in (sys_root / "devices/system/node").glob("node[0-9]*")
            if p.name[4:].isdigit()
        )
        if [p.name for p in self.nodes] != ["node0", "node1"]:
            raise RuntimeError("profile requires exactly NUMA nodes 0 and 1")
        self.governors = sorted(
            (sys_root / "devices/system/cpu/cpufreq").glob("policy*/scaling_governor")
        )
        if not self.governors:
            raise RuntimeError("no CPU frequency policies available to verify performance mode")
        self.numa = proc / "sys/kernel/numa_balancing"
        self.targets = {p: "performance" for p in self.governors}
        self.targets[self.numa] = "0"
        self.targets.update({
            p / "hugepages/hugepages-2048kB/nr_hugepages": str(PAGES_PER_NODE)
            for p in self.nodes
        })
        self.original = {p: p.read_text().strip() for p in self.targets}
        self.limits = {
            key: resource.getrlimit(key)
            for key in (resource.RLIMIT_MEMLOCK, resource.RLIMIT_NOFILE)
        }
        self.changed = []
        self.changed_limits = []

    def snapshot(self):
        return {
            "time_ns": time.time_ns(),
            "governors": {str(p): p.read_text().strip() for p in self.governors},
            "numa_balancing": int(self.numa.read_text()),
            "hugepages_2MiB": hugepages(self.nodes),
            "memlock": resource.getrlimit(resource.RLIMIT_MEMLOCK),
            "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
            "proc_self_limits": (self.proc / "self/limits").read_text(),
        }

    def apply(self, check_interrupt):
        no_fio(self.proc)
        limits = {
            resource.RLIMIT_MEMLOCK: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
            resource.RLIMIT_NOFILE: tuple(
                at_least(v, 65536) for v in self.limits[resource.RLIMIT_NOFILE]
            ),
        }
        for key, value in limits.items():
            check_interrupt()
            self.changed_limits.append(key)
            resource.setrlimit(key, value)
            if resource.getrlimit(key) != value:
                raise RuntimeError(f"resource limit {key} failed verification")
        for path, value in self.targets.items():
            check_interrupt()
            self.changed.append(path)  # A failed write can still partially allocate.
            path.write_text(value + "\n")
            if path.read_text().strip() != value:
                raise RuntimeError(f"setting failed verification: {path}")
        for name, counts in hugepages(self.nodes).items():
            if counts["free_hugepages"] < PAGES_PER_NODE:
                raise RuntimeError(f"{name}: fewer than {PAGES_PER_NODE} free HugeTLB pages")
        check_interrupt()

    def restore(self):
        errors = []
        for path in reversed(self.changed):
            try:
                path.write_text(self.original[path] + "\n")
                if path.read_text().strip() != self.original[path]:
                    raise RuntimeError("readback differs")
            except Exception as error:
                errors.append(f"restore {path}: {error}")
        for key in reversed(self.changed_limits):
            try:
                resource.setrlimit(key, self.limits[key])
                if resource.getrlimit(key) != self.limits[key]:
                    raise RuntimeError("readback differs")
            except Exception as error:
                errors.append(f"restore resource limit {key}: {error}")
        return errors


def sample_processes(pid, proc):
    children = {}
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:
                parent = int((entry / "stat").read_text().rpartition(")")[2].split()[1])
                children.setdefault(parent, []).append(int(entry.name))
            except (OSError, ValueError, IndexError):
                continue
    pending, records = [pid], []
    while pending:
        current = pending.pop()
        pending.extend(children.get(current, []))
        path = proc / str(current)
        record = {"pid": current}
        try:
            record["cmdline"] = (path / "cmdline").read_bytes().decode(errors="replace").strip("\0").split("\0")
            for filename, keys in (
                ("status", {"Name", "Cpus_allowed_list", "Mems_allowed_list"}),
                ("smaps_rollup", {"Private_Hugetlb", "Shared_Hugetlb", "AnonHugePages", "Rss"}),
                ("limits", None),
            ):
                try:
                    text = (path / filename).read_text()
                    record[filename] = text if keys is None else {
                        key: value.strip() for line in text.splitlines()
                        if ":" in line for key, value in [line.split(":", 1)] if key in keys
                    }
                except OSError as error:
                    record[filename] = {"error": str(error)}
        except OSError as error:
            record["error"] = str(error)
        records.append(record)
    return records


def sample_runtime(child, profile, evidence, stop, errors):
    try:
        with (evidence / "runtime.jsonl").open("w") as output:
            while True:
                value = {
                    "time_ns": time.time_ns(),
                    "hugepages_2MiB": hugepages(profile.nodes),
                    "processes": sample_processes(child.pid, profile.proc),
                }
                output.write(json.dumps(value) + "\n")
                output.flush()
                if stop.wait(5):
                    break
    except Exception as error:
        errors.append(str(error))


def stop_group(child, signum=signal.SIGTERM):
    if child is None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


class Interrupted(Exception):
    pass


def verify_host(expected_tag):
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise RuntimeError("requires Linux root; no settings changed")
    tag = Path("/sys/class/dmi/id/product_serial").read_text().strip()
    if tag != expected_tag:
        raise RuntimeError(f"DMI service tag mismatch: {tag!r}")


def run(args):
    verify_host(args.expected_tag)
    if os.environ.get("MC_MMAP_ARENA_POOL_SIZE"):
        raise RuntimeError("inherited MC_MMAP_ARENA_POOL_SIZE must be unset")
    no_fio()
    profile = Profile()
    before = profile.snapshot()
    args.evidence.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment.update(ENVIRONMENT)
    write_json(args.evidence / "command.json", {
        "argv": args.command, "expected_tag": args.expected_tag,
        "environment": {key: environment.get(key) for key in (*ENVIRONMENT, "MC_MMAP_ARENA_POOL_SIZE")},
    })
    write_json(args.evidence / "before.json", before)
    received = []
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    for signum in previous:
        signal.signal(signum, lambda value, frame: received.append(value))

    def check_interrupt():
        if received:
            raise Interrupted(f"received signal {received[0]}")

    result = {"returncode": 1, "command_returncode": None, "restore_errors": []}
    child, sampler = None, None
    stop, sampling_errors = threading.Event(), []
    try:
        profile.apply(check_interrupt)
        write_json(args.evidence / "applied.json", profile.snapshot())
        check_interrupt()
        child = subprocess.Popen(args.command, env=environment, start_new_session=True)
        sampler = threading.Thread(
            target=sample_runtime,
            args=(child, profile, args.evidence, stop, sampling_errors), daemon=True,
        )
        sampler.start()
        while True:
            check_interrupt()
            try:
                result["command_returncode"] = child.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        code = result["command_returncode"]
        result["returncode"] = code if code >= 0 else 128 - code
    except Interrupted as error:
        result.update(returncode=128 + received[0], error=str(error))
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            stop_group(child, received[0] if received else signal.SIGTERM)
            if child is not None:
                result["command_returncode"] = child.returncode
        except Exception as error:
            result["restore_errors"].append(f"stop child group: {error}")
        stop.set()
        if sampler is not None:
            sampler.join(timeout=10)
            if sampler.is_alive():
                sampling_errors.append("runtime sampler did not stop")
        if not (args.evidence / "applied.json").exists():
            try:
                write_json(args.evidence / "applied.json", profile.snapshot())
            except Exception as error:
                result["restore_errors"].append(f"capture partial profile: {error}")
        result["restore_errors"].extend(profile.restore())
        try:
            write_json(args.evidence / "after.json", profile.snapshot())
        except Exception as error:
            result["restore_errors"].append(f"capture restored profile: {error}")
        result["sampling_errors"] = sampling_errors
        if result["restore_errors"] or sampling_errors:
            result["returncode"] = result["returncode"] or 1
        result["finished_time_ns"] = time.time_ns()
        try:
            write_json(args.evidence / "result.json", result)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    if result.get("error") or result["restore_errors"] or sampling_errors:
        print(json.dumps(result), file=sys.stderr)
    return result["returncode"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--evidence", type=Path, required=True, help="new directory; retained as evidence")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command after -- is required")
    try:
        return run(args)
    except (OSError, RuntimeError) as error:
        print(f"system profile: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
