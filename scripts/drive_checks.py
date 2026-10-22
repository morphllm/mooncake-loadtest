#!/usr/bin/env python3
"""Guarded isolated reads and controller short self-tests; never erases devices."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import types


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def load(path):
    return json.loads(path.read_text())


def load_module(path):
    source = path.read_bytes()
    module = types.ModuleType('canonical_drive_acceptance')
    module.__file__ = str(path)
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module, hashlib.sha256(source).hexdigest()


class Commands:
    def __init__(self):
        self.stop = threading.Event()
        self.signum = None
        self.children = {}
        self.lock = threading.RLock()

    def terminate(self, signum, frame=None):
        self.signum = signum
        self.stop.set()
        with self.lock:
            for child in self.children.values():
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        timer = threading.Timer(5, self.kill_remaining)
        timer.daemon = True
        timer.start()

    def kill_remaining(self):
        with self.lock:
            for child in self.children.values():
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def run(self, argv, timeout=30):
        started = now()
        with self.lock:
            if self.stop.is_set():
                raise InterruptedError('interrupted; no further commands issued')
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.children[child.pid] = child
        try:
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                stdout, stderr = child.communicate()
                return dict(argv=argv, returncode=child.returncode, stdout=stdout, stderr=stderr,
                            started=started, finished=now(), timed_out=True)
            return dict(argv=argv, returncode=child.returncode, stdout=stdout, stderr=stderr,
                        started=started, finished=now())
        finally:
            with self.lock:
                self.children.pop(child.pid, None)

    def idle(self, proc=Path('/proc')):
        with self.lock:
            owned = set(self.children)
        for entry in proc.glob('[0-9]*'):
            if int(entry.name) in owned or int(entry.name) == os.getpid():
                continue
            try:
                name = (entry / 'comm').read_text().strip()
                argv = (entry / 'cmdline').read_bytes().split(b'\0')
            except (FileNotFoundError, ProcessLookupError):
                continue
            if (name.startswith(('fio', 'mooncake_', 'transfer_engine'))
                    or (argv and Path(os.fsdecode(argv[0])).name == 'fio')
                    or any(x == b'mooncake.mooncake_store_service' or x.endswith(b'/lib/bench.py') for x in argv)):
                raise RuntimeError(f'active I/O or Mooncake benchmark PID {entry.name}')


def physical(inv):
    if 'nodes' in inv:
        return [dict(device=n['path'], serial=(n.get('serial') or '').strip(), bytes=n['size'])
                for n in inv['nodes'].values()
                if n['type'] == 'disk' and re.fullmatch(r'nvme\d+n\d+', n['kname'])]
    return [dict(device=d['path'], serial=d['serial'], bytes=d['bytes']) for d in inv['drives']]


def keys(entries):
    return sorted((d['device'], d['serial'], d['bytes']) for d in entries)


def validate_inventory(module, expected, actual, tag, count):
    if count < 2 or expected.get('service_tag', expected.get('dmi_tag')) != tag:
        raise RuntimeError('expected inventory/service tag mismatch')
    old, live = physical(expected), physical(actual)
    if (len(old) != count or len(live) != count or keys(old) != keys(live)
            or len({d['serial'] for d in live}) != count):
        raise RuntimeError('full physical drive path/serial/capacity inventory mismatch')
    for d in live:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', d['serial']) or d['bytes'] <= 0:
            raise RuntimeError('invalid physical drive identity')
        node = module.identity(actual, tag, d['device'], d['serial'])
        if node['size'] != d['bytes']:
            raise RuntimeError('canonical namespace capacity mismatch')
    return live


def root_disk(module, inv, node):
    nodes, edges = module.flatten(inv['lsblk'])
    return any('/' in (nodes[name].get('mountpoints') or [])
               for name in module.descendants(node['kname'], edges))


def select(module, inv, args):
    if getattr(args, 'boot_serial', None):
        if args.allowlist or args.acceptance or args.precondition:
            raise RuntimeError('boot reads cannot include data acceptance arguments')
        found = [n for n in inv['nodes'].values()
                 if n['type'] == 'disk' and (n.get('serial') or '').strip() == args.boot_serial]
        if len(found) != 1 or not root_disk(module, inv, found[0]):
            raise RuntimeError('boot serial must uniquely identify a physical root ancestor')
        module.identity(inv, args.expected_tag, found[0]['path'], args.boot_serial)
        return found
    if not args.allowlist:
        raise RuntimeError('data reads require --allowlist')
    allowed = load(args.allowlist)
    if len(allowed) != args.expected_drives - 1:
        raise RuntimeError('data allowlist must contain expected-drives minus one entries')
    return module.targets(inv, args.expected_tag, allowed)


def initialized_extents(module, args, nodes):
    selected = [dict(device=n['path'], serial=n['serial'].strip(), bytes=n['size']) for n in nodes]
    if args.boot_serial:
        return {n['serial'].strip(): (0, n['size']) for n in nodes}, 'BOOT_READ_ONLY_NOT_INITIALIZED_OR_FULL_SURFACE'
    if bool(args.acceptance) == bool(args.precondition):
        raise RuntimeError('data reads require exactly one of --acceptance or --precondition')
    if args.acceptance:
        inv = load(args.acceptance / 'inventory.json')
        if inv['service_tag'] != args.expected_tag:
            raise RuntimeError('acceptance host identity mismatch')
        rows = load(args.acceptance / 'results.json')
        accepted = [dict(device=r['device'], serial=r['serial'], bytes=r['bytes']) for r in rows]
        if keys(accepted) != keys(selected):
            raise RuntimeError('full-acceptance cohort differs from the data allowlist')
        for row in rows:
            directory = args.acceptance / row['serial']
            saved = load(directory / 'result.json')
            if (saved != row or row.get('status') != 'FULL_BYTES_VERIFIED'
                    or row.get('test') != 'FULL_WRITE_CRC32C_READBACK' or row.get('fio_returncode') != 0
                    or row.get('health', {}).get('status') != 'NO_NEW_RECORDED_FAULT'
                    or not module.fio_pass(load(directory / 'fio.json'), row['bytes'], True)
                    or (directory / 'full.fio').read_text() != module.fio_job(row['device'], row['bytes'], True)):
                raise RuntimeError(f"full CRC/raw fio acceptance proof invalid: {row['serial']}")
        return {r['serial']: (0, r['bytes']) for r in rows}, 'DATA_FULL_CAPACITY_CRC_PROVEN_TIMED_READS_ONLY'
    manifest = load(args.precondition)
    if (manifest.get('service_tag') != args.expected_tag or manifest.get('status') != 'COMPLETE_BOUNDED_EXTENTS'
            or manifest.get('full_surface_verified') is not False):
        raise RuntimeError('bounded precondition identity/status mismatch')
    rows = manifest.get('results', [])
    initialized = [dict(device=r['device'], serial=r['serial'], bytes=r['namespace_bytes']) for r in rows]
    if keys(initialized) != keys(selected):
        raise RuntimeError('bounded precondition cohort mismatch')
    offset, size = manifest['initialized_offset'], manifest['initialized_bytes_per_drive']
    if not isinstance(offset, int) or not isinstance(size, int) or offset < 0 or size <= 0:
        raise RuntimeError('invalid initialized extent')
    for row in rows:
        if (row.get('discard_proven') is not True or row.get('discard_bytes') != row['namespace_bytes']
                or row.get('crc_extent_verified') is not True or row.get('initialized_offset') != offset
                or row.get('initialized_bytes') != size or row.get('write_bytes') != size
                or row.get('read_bytes') != size or offset + size > row['namespace_bytes']
                or row.get('full_surface_verified') is not False
                or row.get('health_delta', {}).get('status') != 'NO_NEW_RECORDED_FAULT'):
            raise RuntimeError(f"bounded CRC proof invalid: {row['serial']}")
    return {r['serial']: (offset, size) for r in rows}, 'DATA_BOUNDED_INITIALIZED_EXTENT_NOT_FULL_SURFACE'


def cpulist(value):
    result = set()
    for part in value.strip().split(','):
        bounds = [int(x) for x in part.split('-')]
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def locality(node):
    ctrl = node['kname'].rsplit('n', 1)[0]
    numa = int((Path('/sys/class/nvme') / ctrl / 'device/numa_node').read_text())
    if numa < 0:
        raise RuntimeError('device NUMA locality unavailable')
    eligible = sorted(cpulist(Path(f'/sys/devices/system/node/node{numa}/cpulist').read_text()) & os.sched_getaffinity(0))
    if not eligible:
        raise RuntimeError('no eligible NUMA-local CPU')
    cpu = eligible[-1]
    return numa, cpu, Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor')


def decode(value):
    if value['returncode'] != 0:
        raise RuntimeError(f"command failed: {value.get('stderr', '')}")
    return json.loads(value['stdout'])


def emit(args, rows, metadata):
    save(args.out / 'results.json', dict(metadata=metadata, drives=rows))
    text = ['# Drive checks', '', metadata['scope'], '', '| Serial | Device | Status |', '|---|---|---|']
    text.extend(f"| {r['serial']} | {r['device']} | {r['status']} |" for r in rows)
    (args.out / 'results.md').write_text('\n'.join(text) + '\n')


def read_one(module, args, node, extent, guard, commands, scope):
    serial = node['serial'].strip()
    dest = args.out / serial
    dest.mkdir()
    row = dict(serial=serial, device=node['path'], bytes=node['size'], status='ERROR', scope=scope, speeds_GB_s=[])
    governor, original, before = None, None, None
    stop, samples, sample_errors = threading.Event(), [], []
    def sample():
        while not stop.is_set():
            try:
                result = commands.run(['nvme', 'smart-log', node['path'], '--output-format=json'])
                result['time'] = time.time()
                samples.append(result)
                with (dest / 'samples.jsonl').open('a') as output:
                    output.write(json.dumps(result) + '\n')
            except Exception as error:
                sample_errors.append(str(error))
            stop.wait(5)
    sampler = threading.Thread(target=sample, daemon=True)
    try:
        guard()
        numa, cpu, governor = locality(node)
        row.update(numa=numa, cpu=cpu)
        original = governor.read_text().strip()
        before = module.health(node['path'], dest, 'before')
        governor.write_text('performance\n')
        if governor.read_text().strip() != 'performance':
            raise RuntimeError('performance governor did not apply')
        save(dest / 'governor.json', dict(path=str(governor), original=original, applied='performance'))
        offset, size = extent
        size = size // 131072 * 131072
        if offset % 131072 or size < 131072:
            raise RuntimeError('initialized read extent must support aligned 128 KiB I/O')
        save(dest / 'extent.json', dict(offset=offset, bytes=size, scope=scope))
        sampler.start()
        for rep in range(1, 4):
            guard()
            output = dest / f'fio-{rep}.json'
            argv = ['numactl', f'--physcpubind={cpu}', f'--membind={numa}', 'fio', '--readonly',
                    '--name=isolated-seqread', f'--filename={node["path"]}', '--rw=read', '--ioengine=io_uring',
                    '--direct=1', '--invalidate=1', '--bs=128k', '--iodepth=32', '--numjobs=1', '--thread=1',
                    '--time_based=1', '--runtime=30', '--ramp_time=5', f'--size={size}', f'--offset={offset}',
                    '--allow_file_create=0', '--group_reporting=1', '--eta=never', '--output-format=json', f'--output={output}']
            save(dest / f'command-{rep}.json', argv)
            result = commands.run(argv, timeout=155)
            save(dest / f'execution-{rep}.json', result)
            if result['returncode']:
                raise RuntimeError(f'fio repeat {rep} failed')
            jobs = load(output)['jobs']
            if (len(jobs) != 1 or jobs[0].get('error', 1)
                    or any(jobs[0].get(k, {}).get('io_bytes', 0) for k in ('write', 'trim'))):
                raise RuntimeError('fio job failed or recorded unexpected write/trim bytes')
            rd = jobs[0]['read']
            if rd['io_bytes'] <= 0 or rd['runtime'] < 27000:
                raise RuntimeError('insufficient timed read runtime/bytes')
            speed = rd['io_bytes'] / (rd['runtime'] / 1000) / 1e9
            row['speeds_GB_s'].append(speed)
            row[f'repeat{rep}_GB_s'], row[f'repeat{rep}_bytes'] = speed, rd['io_bytes']
            percentile = rd.get('clat_ns', rd.get('lat_ns', {})).get('percentile', {}).get('99.000000')
            row[f'repeat{rep}_read_p99_ms'] = percentile / 1e6 if percentile is not None else None
        speeds = sorted(row['speeds_GB_s'])
        row.update(status='MEASURED', min_GB_s=speeds[0], median_GB_s=speeds[1], max_GB_s=speeds[2])
    except Exception as error:
        row['error'] = str(error)
    finally:
        stop.set()
        if sampler.ident:
            sampler.join(timeout=35)
        try:
            after = module.health(node['path'], dest, 'after')
            delta = module.smart_delta(before, after)
            save(dest / 'health.json', delta)
            row['health_clean'] = delta['status'] == 'NO_NEW_RECORDED_FAULT'
            initial_smart, final_smart = decode(before['smart']), decode(after['smart'])
            for key in ('critical_warning', 'media_errors', 'percent_used', 'avail_spare', 'spare_thresh', 'num_err_log_entries'):
                row[key] = int(final_smart[key])
            row['num_err_log_entries_delta'] = row['num_err_log_entries'] - int(initial_smart['num_err_log_entries'])
            row['health_clean'] &= (row['critical_warning'] == row['media_errors'] == 0
                                    and row['avail_spare'] >= row['spare_thresh']
                                    and row['percent_used'] < 100 and row['num_err_log_entries_delta'] == 0)
            temperatures = []
            for value in samples:
                temperature = float(decode(value)['temperature'])
                temperatures.append(temperature - 273.15 if temperature > 200 else temperature)
            row['max_nvme_temp_c'] = max(temperatures, default=None)
            if not row['health_clean'] or not samples or sample_errors or sampler.is_alive():
                row.update(status='HEALTH_REVIEW', sampling_errors=sample_errors)
        except Exception as error:
            row.update(status='HEALTH_EVIDENCE_ERROR', health_error=str(error))
        if governor is not None and original is not None:
            try:
                governor.write_text(original + '\n')
                if governor.read_text().strip() != original:
                    raise RuntimeError('governor restore readback differs')
            except Exception as error:
                row.update(status='GOVERNOR_RESTORE_ERROR', restore_error=str(error))
        save(dest / 'result.json', row)
    return row


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def selftest_log(value):
    operation = number(value['Current Device Self-Test Operation'])
    reports = value.get('List of Valid Reports', [])
    if not 0 <= operation <= 15 or not isinstance(reports, list):
        raise ValueError('invalid self-test log')
    return operation, reports


def selftest_verdict(value, before, observed_running):
    operation, reports = selftest_log(value)
    if operation:
        return 'INCOMPLETE_RUNNING'
    if not reports:
        return 'UNKNOWN_NO_REPORT'
    if not observed_running and reports == selftest_log(before)[1]:
        return 'UNKNOWN_NO_NEW_REPORT'
    if number(reports[0]['Self test result']) != 0:
        return 'FAILED_SELFTEST'
    if 'Self test code' in reports[0] and number(reports[0]['Self test code']) != 1:
        return 'UNKNOWN_WRONG_TEST_CODE'
    return 'PASS'


def selftests(module, args, nodes, guard, commands, metadata):
    rows = [dict(serial=n['serial'].strip(), device=n['path'], bytes=n['size'], status='NOT_STARTED',
                 observed_running=False) for n in nodes]
    before, pending = {}, []
    def log(row):
        return commands.run(['nvme', 'self-test-log', row['device'], '--output-format=json'])
    try:
        # Every drive is snapshotted and checked before ANY self-test is started.
        for row in rows:
            dest = args.out / row['serial']
            dest.mkdir()
            before[row['serial']] = module.health(row['device'], dest, 'before')
            initial = log(row)
            save(dest / 'selftest-before.json', initial)
            before[row['serial']]['selftest'] = initial
            identify = decode(before[row['serial']]['identify'])
            row['supported'] = bool(number(identify['oacs']) & (1 << 4))
            if not row['supported']:
                row['status'] = 'UNKNOWN_UNSUPPORTED'
                continue
            if selftest_log(decode(initial))[0]:
                raise RuntimeError(f"preexisting self-test: {row['serial']}; no tests started")
            for value in before[row['serial']].values():
                decode(value)
        emit(args, rows, metadata)
        for row in rows:
            if row['status'] != 'NOT_STARTED':
                continue
            guard()
            current = log(row)
            save(args.out / row['serial'] / 'selftest-prestart.json', current)
            if selftest_log(decode(current))[0]:
                raise RuntimeError('a self-test appeared before launch')
            row['started'] = now()
            result = commands.run(['nvme', 'device-self-test', row['device'], '--self-test-code=1'])
            save(args.out / row['serial'] / 'start.json', result)
            if result['returncode']:
                row['status'] = 'UNKNOWN_START_FAILED'
            else:
                row.update(status='INCOMPLETE_RUNNING', deadline=time.monotonic() + 1800)
                pending.append(row)
            emit(args, rows, metadata)
        while pending:
            guard()
            for row in pending[:]:
                dest = args.out / row['serial']
                result = log(row)
                with (dest / 'polls.jsonl').open('a') as output:
                    output.write(json.dumps(result) + '\n')
                try:
                    value = decode(result)
                    operation, _ = selftest_log(value)
                    row['observed_running'] |= operation == 1
                    if operation and time.monotonic() < row['deadline']:
                        continue
                    after = module.health(row['device'], dest, 'after')
                    final = log(row)
                    save(dest / 'selftest-after.json', final)
                    status = selftest_verdict(decode(final), decode(before[row['serial']]['selftest']), row['observed_running'])
                    if operation:
                        status = 'INCOMPLETE_TIMEOUT_RUNNING'
                    baseline = {k: v for k, v in before[row['serial']].items() if k != 'selftest'}
                    delta = module.smart_delta(baseline, after)
                    save(dest / 'health.json', delta)
                    row['health_clean'] = delta['status'] == 'NO_NEW_RECORDED_FAULT'
                    if status == 'PASS' and not row['health_clean']:
                        status = 'FAILED_HEALTH_DELTA'
                    row.update(status=status, finished=now())
                    pending.remove(row)
                    save(dest / 'result.json', row)
                except Exception as error:
                    row['last_poll_error'] = str(error)
                    if time.monotonic() >= row['deadline']:
                        row.update(status='UNKNOWN_TIMEOUT_EVIDENCE', finished=now())
                        pending.remove(row)
            emit(args, rows, metadata)
            if pending and commands.stop.wait(30):
                raise InterruptedError('monitor interrupted; hardware self-tests left running')
    except Exception as error:
        metadata['error'] = str(error)
    finally:
        for row in rows:
            row.pop('deadline', None)
        metadata.update(finished=now(), signal=commands.signum,
                        status='PASS' if all(r['status'] == 'PASS' for r in rows) else 'INCOMPLETE_OR_FAILED')
        emit(args, rows, metadata)
    return rows


def execute(args, module, module_sha256, commands):
    expected = load(args.inventory)
    def guard():
        if commands.stop.is_set():
            raise InterruptedError('interrupted')
        commands.idle()
        current = module.inventory()
        validate_inventory(module, expected, current, args.expected_tag, args.expected_drives)
        if args.mode == 'reads':
            select(module, current, args)
        return current
    inv = guard()
    nodes = select(module, inv, args) if args.mode == 'reads' else [
        module.identity(inv, args.expected_tag, d['device'], d['serial']) for d in physical(inv)]
    extents, scope = initialized_extents(module, args, nodes) if args.mode == 'reads' else ({}, 'CONTROLLER_SHORT_SELFTEST_NOT_FULL_SURFACE')
    args.out.mkdir(parents=True, exist_ok=False)
    save(args.out / 'inventory.json', inv)
    metadata = dict(started=now(), service_tag=args.expected_tag, mode=args.mode,
                    expected_drives=args.expected_drives, selected_count=len(nodes), scope=scope,
                    module_sha256=module_sha256, module=str(args.module),
                    args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()})
    save(args.out / 'metadata.json', metadata)
    if args.mode == 'selftest':
        rows = selftests(module, args, nodes, guard, commands, metadata)
        good = all(r['status'] == 'PASS' for r in rows)
    else:
        rows = []
        for node in nodes:
            rows.append(read_one(module, args, node, extents[node['serial'].strip()], guard, commands, scope))
            emit(args, rows, metadata)
            if commands.stop.is_set() or rows[-1]['status'] == 'GOVERNOR_RESTORE_ERROR':
                break
        metadata['finished'] = now()
        good = len(rows) == len(nodes) and all(r['status'] == 'MEASURED' and r.get('health_clean') for r in rows)
    metadata['status'] = 'PASS' if good else 'INCOMPLETE_OR_FAILED'
    emit(args, rows, metadata)
    return 128 + commands.signum if commands.signum else (0 if good else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    for mode in ('reads', 'selftest'):
        command = sub.add_parser(mode)
        command.add_argument('--module', required=True, type=Path)
        command.add_argument('--expected-tag', required=True)
        command.add_argument('--expected-drives', type=int, default=24)
        command.add_argument('--inventory', required=True, type=Path)
        command.add_argument('--out', required=True, type=Path)
        command.add_argument('--allowlist', type=Path)
        if mode == 'reads':
            command.add_argument('--boot-serial')
            proof = command.add_mutually_exclusive_group()
            proof.add_argument('--acceptance', type=Path)
            proof.add_argument('--precondition', type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Linux root required; no device commands issued')
    commands = Commands()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, commands.terminate)
    try:
        module, digest = load_module(args.module)
        module.command, module.save = commands.run, save
        return execute(args, module, digest, commands)
    except Exception as error:
        print(f'drive checks: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
