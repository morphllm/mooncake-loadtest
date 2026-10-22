#!/usr/bin/env python3
"""Plan, provision, resume, or clean up a UUID-owned temporary NVMe test pool."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


class Refusal(RuntimeError):
    pass


def save(path, value):
    """Publish a complete, durable manifest before proceeding to another step."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rows(text, section):
    return [row for report in json.loads(text).get('report', []) for row in report.get(section, [])]


def geometry(node, sysroot=Path('/sys')):
    match = re.fullmatch(r'(nvme\d+)n\d+', node['kname'])
    if not match:
        raise Refusal('only whole NVMe namespaces are supported')
    numa = int((sysroot / 'class/nvme' / match[1] / 'device/numa_node').read_text())
    sector = int((sysroot / 'class/block' / node['kname'] / 'queue/logical_block_size').read_text())
    if numa < 0 or sector not in (512, 4096):
        raise Refusal(f'unsupported NUMA/sector geometry: {node["path"]}')
    return numa, sector


def groups_for(entries, geometries, operation, size_gib):
    grouped = {}
    for entry in entries:
        grouped.setdefault(tuple(geometries[entry['device']]), []).append(entry['device'])
    groups = []
    for (numa, sector), devices in sorted(grouped.items()):
        name = f'numa{numa}-sector{sector}'
        vg = f'mcl_{operation}_n{numa}_s{sector}'
        groups.append(dict(name=name, numa=numa, sector=sector, devices=sorted(devices),
            size_gib=size_gib, targets=[dict(e) for e in entries if e['device'] in devices],
            vg=vg, device=f'/dev/{vg}/data',
            mount=f'/mnt/mooncake-data/{operation}/{name}', vg_uuid=None, lv_uuid=None,
            fs_uuid=None, mounted=False, lv_removed=False, vg_removed=False))
    return groups


def validate_layout(found, group):
    if len(found) != 1:
        raise Refusal('expected exactly one LV segment')
    row = found[0]
    devices = {re.sub(r'\(\d+\)$', '', part.strip()) for part in row['devices'].split(',')}
    count = len(group['devices'])
    extent = (4 << 20) * count
    expected_bytes = ((group['size_gib'] * (1 << 30) + extent - 1) // extent) * extent
    if (row['lv_path'].strip() != group['device'] or devices != set(group['devices'])
            or row['segtype'].strip() != ('linear' if count == 1 else 'striped')
            or int(row['stripes']) != count or int(float(row['lv_size'])) != expected_bytes
            or (count > 1 and int(float(row['stripe_size'])) != 256 << 10)):
        raise Refusal(f'LV layout differs from plan: {group["name"]}')
    if group['lv_uuid'] and row['lv_uuid'].strip() != group['lv_uuid']:
        raise Refusal('LV UUID changed')


def validate_metadata(state, pvs, vgs, segments):
    """Never discover/adopt ownership from live objects during recovery."""
    entries = {entry['device']: entry for entry in state['entries']}
    seen = {}
    expected_ids = {entry['pv_uuid']: path for path, entry in entries.items() if entry['pv_uuid']}
    if len(expected_ids) != sum(bool(e['pv_uuid']) for e in entries.values()):
        raise Refusal('duplicate recorded PV UUID')
    for pv in pvs:
        path, identity = pv['pv_name'].strip(), pv['pv_uuid'].strip()
        if identity in expected_ids and expected_ids[identity] != path:
            raise Refusal('recorded PV UUID appears on another device')
        if path in entries:
            if path in seen:
                raise Refusal('ambiguous PV path')
            seen[path] = pv
    for path, entry in entries.items():
        actual = seen.get(path)
        if entry['pv_removed'] or not entry['pv_uuid']:
            if actual:
                raise Refusal(f'unrecorded PV exists: {path}')
        elif not actual or actual['pv_uuid'].strip() != entry['pv_uuid']:
            raise Refusal(f'PV UUID changed or missing: {path}')
    for group in state['groups']:
        name = group['vg']
        found = [v for v in vgs if v['vg_name'].strip() == name]
        live_vg = bool(group['vg_uuid']) and not group['vg_removed']
        if not live_vg:
            if found:
                raise Refusal(f'unrecorded VG exists: {name}')
        elif len(found) != 1 or found[0]['vg_uuid'].strip() != group['vg_uuid']:
            raise Refusal(f'VG UUID changed or missing: {name}')
        members = {pv['pv_name'].strip() for pv in pvs if pv['vg_name'].strip() == name}
        if members != (set(group['devices']) if live_vg else set()):
            raise Refusal(f'VG membership differs: {name}')
        for path in group['devices']:
            if path in seen:
                wanted = name if live_vg else ''
                if seen[path]['vg_name'].strip() != wanted or (live_vg and seen[path]['vg_uuid'].strip() != group['vg_uuid']):
                    raise Refusal(f'PV belongs to an unowned VG: {path}')
        found_lv = [v for v in segments if v['vg_name'].strip() == name]
        if group['lv_uuid'] and not group['lv_removed']:
            validate_layout(found_lv, group)
        elif found_lv:
            raise Refusal(f'unrecorded LV exists: {name}')


def foreign_fio(proc=Path('/proc')):
    result = []
    for directory in proc.glob('[0-9]*'):
        try:
            name = (directory / 'comm').read_text().strip()
            argv = (directory / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        if name.startswith('fio') or (argv and Path(os.fsdecode(argv[0])).name == 'fio'):
            result.append(directory.name)
    return result


class Pool:
    def __init__(self, args):
        self.args, self.out = args, args.out
        self.manifest = self.out / 'manifest.json'
        self.journal = self.out / 'commands.jsonl'
        self.module_sha = hashlib.sha256(args.module.read_bytes()).hexdigest()
        spec = importlib.util.spec_from_file_location('drive_acceptance', args.module)
        self.drive = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.drive)
        incoming = json.loads(args.allowlist.read_text())
        if self.out.exists():
            if not args.resume:
                raise Refusal('output exists; inspect it and explicitly use --resume')
            self.state = json.loads(self.manifest.read_text())
            original = [{k: e[k] for k in ('device', 'serial', 'bytes')} for e in self.state['entries']]
            if (incoming != original or args.expected_tag != self.state['expected_tag']
                    or args.expected_drives != self.state['expected_drives']
                    or self.module_sha != self.state['module_sha256']
                    or any(g['size_gib'] != args.size_gib for g in self.state['groups'])):
                raise Refusal('resume inputs or canonical module differ from recorded plan')
            if self.state.get('inflight'):
                raise Refusal('interrupted mutation has unresolved ownership; preserve for operator review')
        else:
            if args.resume or args.cleanup:
                raise Refusal('resume/cleanup requires an existing manifest')
            self.out.mkdir(parents=True, exist_ok=False)
            inv = self.drive.inventory()
            selected = self.drive.targets(inv, args.expected_tag, incoming)
            if len(incoming) != args.expected_drives - 1:
                raise Refusal('allowlist must contain expected-drives minus the protected boot drive')
            geo = {n['path']: geometry(n) for n in selected}
            self.state = dict(version=1, operation=uuid.uuid4().hex[:12], status='planned',
                expected_tag=args.expected_tag, expected_drives=args.expected_drives,
                module_sha256=self.module_sha, entries=[dict(e, pv_uuid=None, pv_removed=False) for e in incoming],
                geometries=geo, groups=[], created_dirs={}, inflight=None,
                deletion_condition='Remove only the recorded UUID-owned test storage after harvesting results.')
            self.state['groups'] = groups_for(incoming, geo, self.state['operation'], args.size_gib)
            save(self.out / 'inventory.initial.json', inv)
            self.persist()

    def persist(self):
        save(self.manifest, self.state)

    def run(self, argv, acceptable=(0,)):
        with self.journal.open('a') as stream:
            stream.write(json.dumps(dict(time=time.time(), argv=argv, event='started')) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
        with self.journal.open('a') as stream:
            stream.write(json.dumps(dict(time=time.time(), argv=argv, event='finished',
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)) + '\n')
            stream.flush(); os.fsync(stream.fileno())
        if result.returncode not in acceptable:
            raise Refusal(f'command failed ({result.returncode}): {argv}; see commands.jsonl')
        return result

    def report(self, tool, columns, section, *extra):
        return rows(self.run([tool, *extra, '--reportformat', 'json', '-o', columns]).stdout, section)

    def metadata(self):
        return (self.report('pvs', 'pv_name,pv_uuid,vg_name,vg_uuid', 'pv'),
                self.report('vgs', 'vg_name,vg_uuid', 'vg'),
                self.report('lvs', 'vg_name,lv_name,lv_uuid,lv_path,lv_size,segtype,stripes,stripe_size,devices',
                            'seg', '--segments', '--units', 'b', '--nosuffix'))

    def filesystem(self, device):
        result = self.run(['blkid', '-p', '-o', 'export', device], (0, 2))
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)

    def guard(self):
        deadline = time.monotonic() + 30
        while True:
            self.run(['udevadm', 'settle', '--timeout=5'])
            if foreign_fio():
                raise Refusal('fio is still running')
            inv = self.drive.inventory()
            physical = [n for n in inv['nodes'].values() if n['type'] == 'disk' and re.fullmatch(r'nvme\d+n\d+', n['kname'])]
            if len(physical) != self.state['expected_drives']:
                raise Refusal('physical NVMe count changed')
            for entry in self.state['entries']:
                n = self.drive.identity(inv, self.state['expected_tag'], entry['device'], entry['serial'])
                if n['size'] != entry['bytes'] or list(geometry(n)) != list(self.state['geometries'][entry['device']]):
                    raise Refusal('physical identity, capacity or topology changed')
            validate_metadata(self.state, *self.metadata())
            active_lvs = {g['device']: g for g in self.state['groups'] if g['lv_uuid'] and not g['lv_removed']}
            expected_holders = {}
            _, edges = self.drive.flatten(inv['lsblk'])
            for device, group in active_lvs.items():
                candidates = [n for n in inv['nodes'].values() if os.path.realpath(n['path']) == os.path.realpath(device)]
                if len(candidates) != 1:
                    raise Refusal('owned LV not uniquely identified')
                lv = candidates[0]
                for path in group['devices']:
                    expected_holders[Path(path).name] = {lv['kname']}
                mounts = [m for m in (lv.get('mountpoints') or []) if m]
                if mounts != ([group['mount']] if group['mounted'] else []):
                    raise Refusal('unexpected or missing owned mount')
                fs = self.filesystem(device)
                if group['fs_uuid']:
                    if fs.get('UUID') != group['fs_uuid'] or fs.get('TYPE') != 'xfs':
                        raise Refusal('filesystem UUID/type changed')
                elif fs:
                    raise Refusal('unrecorded filesystem exists on new LV')
            for entry in self.state['entries']:
                name = Path(entry['device']).name
                allowed = {name} | expected_holders.get(name, set())
                if self.drive.descendants(name, edges) != allowed:
                    raise Refusal(f'unexpected block-device descendants: {name}')
            relevant = set(expected_holders)
            relevant.update(Path(e['device']).name for e in self.state['entries'])
            for device in active_lvs:
                relevant.update(n['kname'] for n in inv['nodes'].values() if os.path.realpath(n['path']) == os.path.realpath(device))
            for name in relevant:
                expected = expected_holders.get(name, set())
                if set(inv['holders'].get(name, [])) != expected:
                    raise Refusal(f'unexpected holders: {name}')
                node = inv['nodes'][name]
                if node.get('ro'):
                    raise Refusal(f'read-only device: {name}')
            opens = {n: inv['open_block_devices'][n] for n in relevant if inv['open_block_devices'].get(n)}
            if opens:
                evidence = dict(time=time.time(), devices=opens, processes={})
                for pid in {p for ids in opens.values() for p in ids}:
                    try:
                        evidence['processes'][pid] = Path('/proc', str(pid), 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                    except FileNotFoundError:
                        evidence['processes'][pid] = 'exited before capture'
                with (self.out / 'open-observations.jsonl').open('a') as output:
                    output.write(json.dumps(evidence) + '\n')
                if time.monotonic() >= deadline:
                    raise Refusal('devices remain open; evidence preserved')
                time.sleep(1)
                continue
            pending = [e for e in self.state['entries'] if Path(e['device']).name not in expected_holders]
            if pending:
                self.drive.targets(inv, self.state['expected_tag'], pending)
            save(self.out / 'inventory.latest.json', inv)
            return inv

    def mutate(self, argv, record):
        self.guard()
        self.state['inflight'] = dict(argv=argv, started=time.time())
        self.persist()
        self.run(argv)
        record()
        self.state['inflight'] = None
        self.persist()

    def provision(self):
        for entry in self.state['entries']:
            if entry['pv_uuid']:
                continue
            path = entry['device']
            if json.loads(self.run(['wipefs', '--json', '--no-act', path]).stdout).get('signatures'):
                raise Refusal(f'existing signature on {path}')
            def record_pv():
                found = [p for p in self.metadata()[0] if p['pv_name'].strip() == path]
                if len(found) != 1 or found[0]['vg_name'].strip():
                    raise Refusal('new PV identity unavailable')
                entry['pv_uuid'] = found[0]['pv_uuid'].strip()
            self.mutate(['pvcreate', '--zero', 'n', '--metadatasize', '1m', path], record_pv)
        for group in self.state['groups']:
            if not group['vg_uuid']:
                def record_vg():
                    found = [v for v in self.metadata()[1] if v['vg_name'].strip() == group['vg']]
                    if len(found) != 1:
                        raise Refusal('new VG identity unavailable')
                    group['vg_uuid'] = found[0]['vg_uuid'].strip()
                self.mutate(['vgcreate', '--physicalextentsize', '4m', group['vg'], *group['devices']], record_vg)
        for group in self.state['groups']:
            if not group['lv_uuid']:
                args = ['lvcreate', '--size', f"{group['size_gib']}g", '--name', 'data', '--zero', 'n', '--wipesignatures', 'n']
                args += (['--type', 'linear'] if len(group['devices']) == 1 else
                         ['--stripes', str(len(group['devices'])), '--stripesize', '256k'])
                def record_lv():
                    found = [v for v in self.metadata()[2] if v['vg_name'].strip() == group['vg']]
                    validate_layout(found, group)
                    group['lv_uuid'] = found[0]['lv_uuid'].strip()
                    group['layout'] = found[0]
                self.mutate([*args, group['vg']], record_lv)
        for group in self.state['groups']:
            if not group['fs_uuid']:
                if int(self.run(['blockdev', '--getss', group['device']]).stdout) != group['sector']:
                    raise Refusal('LV logical sector does not match its homogeneous group')
                def record_fs():
                    fs = self.filesystem(group['device'])
                    if not fs.get('UUID') or fs.get('TYPE') != 'xfs':
                        raise Refusal('new filesystem identity unavailable')
                    group['fs_uuid'] = fs['UUID']
                self.mutate(['mkfs.xfs', '-s', f"size={group['sector']}", '-L', 'mcltest', group['device']], record_fs)
            if not group['mounted']:
                self.make_directory(Path(group['mount']))
                self.mutate(['mount', '-t', 'xfs', '-o', 'noatime', group['device'], group['mount']],
                            lambda: group.update(mounted=True))
        self.state['status'] = 'mounted'
        self.persist()

    def make_directory(self, path):
        if path.exists():
            identity = self.state['created_dirs'].get(str(path))
            st = path.stat()
            if identity != [st.st_dev, st.st_ino] or not path.is_dir() or path.is_mount() or any(path.iterdir()):
                raise Refusal(f'unowned, replaced or occupied mount directory: {path}')
            return
        for parent in path.parents:
            if str(parent) in self.state['created_dirs']:
                st = parent.stat()
                if [st.st_dev, st.st_ino] != self.state['created_dirs'][str(parent)] or parent.is_mount():
                    raise Refusal(f'owned parent directory changed: {parent}')
        if not path.parent.exists():
            self.make_directory(path.parent)
        path.mkdir()
        st = path.stat()
        self.state['created_dirs'][str(path)] = [st.st_dev, st.st_ino]
        self.persist()

    def cleanup(self):
        for group in reversed(self.state['groups']):
            if group['mounted']:
                self.mutate(['umount', group['mount']], lambda: group.update(mounted=False))
            if group['lv_uuid'] and not group['lv_removed']:
                self.mutate(['lvremove', '--yes', group['device']], lambda: group.update(lv_removed=True))
            if group['vg_uuid'] and not group['vg_removed']:
                self.mutate(['vgremove', '--yes', group['vg']], lambda: group.update(vg_removed=True))
        for entry in self.state['entries']:
            if entry['pv_uuid'] and not entry['pv_removed']:
                self.mutate(['pvremove', '--yes', entry['device']], lambda: entry.update(pv_removed=True))
        retained = []
        for path, identity in sorted(self.state['created_dirs'].items(), key=lambda item: -len(Path(item[0]).parts)):
            directory = Path(path)
            if not directory.exists():
                raise Refusal(f'owned directory disappeared: {path}')
            st = directory.stat()
            if [st.st_dev, st.st_ino] != identity or directory.is_mount():
                raise Refusal(f'directory ownership changed: {path}')
            try:
                directory.rmdir()
            except OSError:
                retained.append(path)
            else:
                del self.state['created_dirs'][path]
                self.persist()
        self.state.update(status='cleaned' if not retained else 'cleaned_directories_retained', retained=retained)
        self.persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-tag', required=True)
    parser.add_argument('--expected-drives', type=int, default=24)
    parser.add_argument('--allowlist', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--size-gib', type=int, default=128, help='size of each homogeneous NUMA/sector group')
    parser.add_argument('--module', type=Path, default=Path('/opt/mooncake-loadtest/lib/drive_acceptance.py'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Linux root required')
    if args.expected_drives < 2 or args.size_gib < 1 or (args.cleanup and not args.resume):
        parser.error('positive pool size, at least two drives, and --resume for cleanup required')
    required = ['udevadm', 'pvs', 'vgs', 'lvs', 'pvcreate', 'vgcreate', 'lvcreate', 'wipefs',
                'mkfs.xfs', 'blkid', 'blockdev', 'mount', 'umount', 'lvremove', 'vgremove', 'pvremove']
    if missing := [tool for tool in required if not shutil.which(tool)]:
        parser.error(f'missing required tools: {missing}')
    lock = open('/run/lock/mooncake-storage-pool.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pool = Pool(args)
    try:
        pool.guard()
        if args.apply:
            if args.cleanup:
                pool.cleanup()
            else:
                if any(e['pv_removed'] for e in pool.state['entries']):
                    raise Refusal('cleanup has begun; cannot reprovision this operation')
                pool.provision()
        print(json.dumps(pool.state, indent=2))
    except Exception as exc:
        pool.state.update(status='failed_preserve_for_review', error=str(exc),
            retained_resources={
                'pvs': [e['device'] for e in pool.state['entries'] if e['pv_uuid'] and not e['pv_removed']],
                'lvs': [g['device'] for g in pool.state['groups'] if g['lv_uuid'] and not g['lv_removed']],
                'vgs': [g['vg'] for g in pool.state['groups'] if g['vg_uuid'] and not g['vg_removed']],
                'directories': list(pool.state['created_dirs']), 'inflight': pool.state.get('inflight')})
        pool.persist()
        raise


if __name__ == '__main__':
    main()
