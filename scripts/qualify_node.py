#!/usr/bin/env python3
"""Persistent qualification of one explicitly identified Linux storage server."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def normalize_node(value, complete=True):
    """One shared schema for the SSH coordinator and the node process."""
    if not isinstance(value, dict):
        raise ValueError('node must be an object')
    node = dict(value)
    for key in ('name', 'ssh', 'expected_tag'):
        text = node.get(key)
        if not isinstance(text, str) or not text or text.startswith('-') or re.search(r'[\s\x00-\x1f]', text):
            raise ValueError(f'invalid node {key}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}', node['name']):
        raise ValueError('node name must be a short path-safe identifier')
    node.setdefault('sudo', False)
    node.setdefault('expected_drives', 24)
    node.setdefault('allowlist', [])
    node.setdefault('protocol', 'rdma')
    node.setdefault('require_rdma', True)
    node.setdefault('nics', [])
    node.setdefault('profile', 'standard')
    for key, default in [('pool_gib', 128), ('segment_gib', 8), ('disk_cap_gib', 64),
                         ('workers', 16), ('batch', 4), ('offload_heartbeat_seconds', 1), ('minimum_read_gbps', 5.5)]:
        node.setdefault(key, default)
        if isinstance(node[key], bool) or not isinstance(node[key], (int, float)) or not math.isfinite(node[key]) or node[key] <= 0:
            raise ValueError(f'{key} must be positive')
    if not isinstance(node['expected_drives'], int) or isinstance(node['expected_drives'], bool) or node['expected_drives'] < 2:
        raise ValueError('expected_drives must be at least two')
    if not isinstance(node['sudo'], bool) or not isinstance(node['require_rdma'], bool):
        raise ValueError('sudo and require_rdma must be booleans')
    if node['protocol'] not in ('rdma', 'tcp') or node['profile'] not in ('quick', 'standard', 'soak'):
        raise ValueError('explicit supported protocol/profile required')
    if not isinstance(node['nics'], list) or any(not isinstance(x, str) or not re.fullmatch(r'[\w.-]+', x) for x in node['nics']):
        raise ValueError('nics must be a list of device names')
    entries = node['allowlist']
    if not isinstance(entries, list):
        raise ValueError('allowlist must be an explicit array')
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {'device', 'serial', 'bytes'}
                or not re.fullmatch(r'/dev/nvme\d+n\d+', str(entry['device']))
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(entry['serial']))
                or not isinstance(entry['bytes'], int) or isinstance(entry['bytes'], bool) or entry['bytes'] <= 0):
            raise ValueError('each data drive needs a whole NVMe path, serial and exact positive byte count')
    if len({e['device'] for e in entries}) != len(entries) or len({e['serial'] for e in entries}) != len(entries):
        raise ValueError('duplicate data drive path or serial')
    if complete:
        if len(entries) != node['expected_drives'] - 1:
            raise ValueError('reviewed allowlist must contain every data drive and exclude exactly one boot drive')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', str(node.get('boot_serial', ''))):
            raise ValueError('boot_serial is required')
        if node['boot_serial'] in {e['serial'] for e in entries}:
            raise ValueError('boot serial is in the writable allowlist')
        wheel = node.get('wheel', {})
        if (not isinstance(wheel, dict) or wheel.get('flavor') not in ('non-cuda', 'cuda12', 'cuda13')
                or not isinstance(wheel.get('version'), str) or not wheel['version']
                or not re.fullmatch(r'[0-9a-f]{64}', str(wheel.get('sha256', '')))
                or bool(wheel.get('file')) == bool(wheel.get('url'))):
            raise ValueError('pin wheel flavor/version/SHA256 and exactly one remote file or HTTPS URL')
        if wheel.get('url'):
            url = urlparse(wheel['url'])
            if url.scheme != 'https' or not url.netloc or url.username or url.password:
                raise ValueError('wheel URL must be HTTPS without embedded credentials')
        if wheel.get('file') and (not isinstance(wheel['file'], str) or not wheel['file'].startswith('/')):
            raise ValueError('wheel.file is an absolute path already on the remote node')
    for key in ('pool_gib', 'segment_gib', 'disk_cap_gib', 'workers', 'batch', 'offload_heartbeat_seconds'):
        if not isinstance(node[key], int):
            raise ValueError(f'{key} must be an integer')
    return node


validate_node = normalize_node


def load_module(path):
    spec = importlib.util.spec_from_file_location('qualification_drive_acceptance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_crc(path, entries, module, writable=True):
    """Reopen raw fio evidence; wrapper exit status alone is not acceptance."""
    for entry in entries:
        directory = Path(path) / entry['serial']
        result_path = directory / 'result.json'
        result = json.loads(result_path.read_text())
        if (result.get('device'), result.get('serial'), result.get('bytes')) != (entry['device'], entry['serial'], entry['bytes']):
            return False
        if result.get('status') != ('FULL_BYTES_VERIFIED' if writable else 'FULL_BYTES_READ'):
            return False
        if result.get('health', {}).get('status') != 'NO_NEW_RECORDED_FAULT':
            return False
        if not module.fio_pass(json.loads((directory / 'fio.json').read_text()), entry['bytes'], writable):
            return False
    return True


def mooncake_verdict(path):
    document = json.loads((Path(path) / 'report.json').read_text())
    rows = document.get('rows', [])
    checks = {row.get('check', ''): row for row in rows}
    lanes = json.loads((Path(path) / 'server/info.json').read_text()).get('lanes', [])
    evidence = all(
        checks.get(f'lane {lane["idx"]} master: SSD hits', {}).get('verdict') == 'PASS'
        and checks.get(f'lane {lane["idx"]} master: SSD bytes', {}).get('verdict') == 'PASS'
        and any(row.get('check', '').startswith(f'lane {lane["idx"]} L3 read attribution')
                and row.get('verdict') == 'PASS' for row in rows)
        for lane in lanes
    )
    return dict(passed=bool(rows and lanes and evidence) and not any(r.get('verdict') == 'FAIL' for r in rows),
                l3_evidence=bool(lanes and evidence), remote_rdma='NOT_QUALIFIED_SINGLE_HOST',
                failures=[r for r in rows if r.get('verdict') == 'FAIL'])


def cohort_complete(path, serials):
    """Accept finished observations, including failed tests, but never partial cohorts."""
    document = json.loads(Path(path).read_text())
    rows = document.get('drives', [])
    return (len(rows) == len(serials) and {r.get('serial') for r in rows} == set(serials)
            and all(r.get('status') and not r['status'].startswith('INCOMPLETE') for r in rows))


def profile_complete(path, report):
    profile = json.loads(Path(path).read_text())
    rows = json.loads(Path(report).read_text()).get('rows', [])
    expected = 1 if any(r.get('verdict') == 'FAIL' for r in rows) else 0
    return (bool(rows) and not profile.get('error') and not profile.get('restore_errors')
            and not profile.get('sampling_errors') and profile.get('command_returncode') == expected
            and profile.get('returncode') == expected)


class Workflow:
    def __init__(self, config, out, resume=False):
        self.config, self.out = config, Path(out)
        self.runtime = self.out.parent / 'runtime'
        self.environment = dict(os.environ, MCL_WORK=str(self.runtime),
                                MOONCAKE_OFFLOAD_HEARTBEAT_INTERVAL_SECONDS=str(config.get('offload_heartbeat_seconds', 1)))
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        state_file = self.out / 'state.json'
        if state_file.exists():
            if not resume:
                raise RuntimeError('existing run requires --resume; never overwrite evidence')
            self.state = json.loads(state_file.read_text())
            if self.state['config_sha256'] != fingerprint:
                raise RuntimeError('configuration changed; use a fresh run')
        else:
            if self.out.exists():
                raise RuntimeError('output exists without an owned state manifest')
            self.out.mkdir(parents=True)
            self.state = dict(schema=1, config_sha256=fingerprint, config=config, steps={}, status='created',
                              created=time.time(), runtime=str(self.runtime))
            self.persist()
        self.module = None

    def persist(self):
        self.state['updated'] = time.time()
        save(self.out / 'state.json', self.state)

    def command(self, argv, log):
        with Path(log).open('a') as stream:
            return subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, env=self.environment).returncode

    def prepare(self, install=False):
        source = {str(p.relative_to(ROOT)): digest(p) for p in
                  [ROOT / 'mooncake-loadtest.sh', *SCRIPTS.glob('*.py')]}
        if self.state.get('source') and self.state['source'] != source:
            raise RuntimeError('source changed during an owned run; restore the recorded bundle')
        command = 'set -e; source "$1"; ' + ('install_system; ' if install else '') + 'mkdir -p "$LIB"; write_payload'
        if self.command(['bash', '-c', command, 'qualify', str(ROOT / 'mooncake-loadtest.sh')], self.out / 'prepare.log'):
            raise RuntimeError('cannot prepare canonical runtime; see prepare.log')
        self.module = load_module(self.runtime / 'lib/drive_acceptance.py')
        self.state['source'] = source
        self.persist()

    def stage(self, name, argv, evidence, validate=None, allow_failure=False, recover_pool=False):
        old = self.state['steps'].get(name)
        evidence = [Path(p) for p in evidence]
        if old:
            if old['status'] == 'complete':
                if any(not Path(p).is_file() or digest(p) != sha for p, sha in old['evidence'].items()):
                    raise RuntimeError(f'{name}: completed evidence changed or disappeared')
                if validate and not validate():
                    raise RuntimeError(f'{name}: completed evidence no longer validates')
                return old['returncode']
            if not recover_pool:
                # A command may finish before the coordinator writes its checkpoint.
                if validate and all(p.is_file() for p in evidence) and validate():
                    self.state['steps'][name] = dict(old, status='complete', returncode=None,
                        recovered_from_evidence=True, evidence={str(p): digest(p) for p in evidence})
                    self.persist()
                    return None
                raise RuntimeError(f'{name}: incomplete/failed stage preserved; inspect it before starting a new run')
            argv = [*argv, '--resume']
        self.state.update(status='running', current_stage=name)
        self.state['steps'][name] = dict(status='running', argv=argv, started=time.time())
        self.persist()
        rc = self.command(argv, self.out / f'{name}.log')
        valid = all(p.is_file() for p in evidence) and (validate() if validate else True)
        record = self.state['steps'][name]
        record.update(status='complete' if valid and (rc == 0 or allow_failure) else 'failed',
                      returncode=rc, finished=time.time(), evidence={str(p): digest(p) for p in evidence if p.is_file()})
        self.persist()
        if record['status'] != 'complete':
            raise RuntimeError(f'{name} failed; see {name}.log and retained evidence')
        return rc

    def inventory(self):
        path = self.out / 'inventory'
        self.stage('inventory', [sys.executable, str(self.runtime / 'lib/drive_acceptance.py'),
                   'inventory', '--out', str(path)], [path / 'inventory.json'],
                   lambda: bool(json.loads((path / 'inventory.json').read_text()).get('nodes')))
        inv = self.module.inventory()
        if inv['service_tag'] != self.config['expected_tag']:
            raise RuntimeError('live DMI identity mismatch')
        physical = [n for n in inv['nodes'].values() if n['type'] == 'disk' and re.fullmatch(r'nvme\d+n\d+', n['kname'])]
        if len(physical) != self.config['expected_drives']:
            raise RuntimeError('physical drive count mismatch')
        for entry in self.config['allowlist']:
            current = self.module.identity(inv, self.config['expected_tag'], entry['device'], entry['serial'])
            if current['size'] != entry['bytes']:
                raise RuntimeError('live data-drive capacity changed')
        return inv

    def check_boot(self, inv):
        boot = [n for n in inv['nodes'].values() if n['type'] == 'disk' and n['serial'].strip() == self.config['boot_serial']]
        if len(boot) != 1:
            raise RuntimeError('boot serial is missing or ambiguous')
        _, edges = self.module.flatten(inv['lsblk'])
        if not any('/' in (inv['nodes'][n].get('mountpoints') or []) for n in self.module.descendants(boot[0]['kname'], edges)):
            raise RuntimeError('configured boot serial does not back mounted root')
        return dict(device=boot[0]['path'], serial=boot[0]['serial'].strip(), bytes=boot[0]['size'])

    def execute(self, inventory_only=False, plan=False):
        c = self.config
        self.prepare(install=not (inventory_only or plan))
        inv = self.inventory()
        if inventory_only:
            self.state.update(status='inventory_complete', current_stage=None)
            self.persist()
            return
        boot = self.check_boot(inv)
        load_module(SCRIPTS / 'drive_checks.py').Commands().idle()
        host_profile = load_module(SCRIPTS / 'system_profile.py').Profile()
        save(self.out / 'profile-preflight.json', host_profile.snapshot())
        missing = [tool for tool in ('pvs', 'vgs', 'lvs', 'pvcreate', 'vgcreate', 'lvcreate', 'mkfs.xfs',
                   'udevadm', 'blkid', 'blockdev', 'mount', 'umount', 'lvremove', 'vgremove', 'pvremove')
                   if not shutil.which(tool)]
        if missing:
            raise RuntimeError(f'pool tools missing before any erase: {missing}')
        allowlist = self.out / 'allowlist.json'
        if not allowlist.exists():
            save(allowlist, c['allowlist'])
        elif json.loads(allowlist.read_text()) != c['allowlist']:
            raise RuntimeError('saved allowlist changed')
        module_path = str(self.runtime / 'lib/drive_acceptance.py')
        common = ['--expected-tag', c['expected_tag'], '--allowlist', str(allowlist)]
        if plan:
            self.module.targets(inv, c['expected_tag'], c['allowlist'])
            self.stage('plan', [sys.executable, module_path, 'plan', *common, '--out', str(self.out / 'plan')],
                       [self.out / 'plan/inventory.json', self.out / 'plan/approved-targets.json'])
            self.state.update(status='plan_complete', current_stage=None)
            self.persist()
            return
        crc = self.out / 'data-crc'
        if 'data-crc' not in self.state['steps']:
            self.module.targets(inv, c['expected_tag'], c['allowlist'])
        self.stage('data-crc', [sys.executable, module_path, 'run', *common, '--apply',
                   '--parallel', str(min(c['expected_drives'] - 1, 24)), '--out', str(crc)],
                   [crc / 'results.json'], lambda: verified_crc(crc, c['allowlist'], self.module))
        check_common = ['--module', module_path, '--expected-tag', c['expected_tag'],
                        '--expected-drives', str(c['expected_drives']), '--inventory', str(self.out / 'inventory/inventory.json')]
        reads = self.out / 'data-reads'
        self.stage('data-reads', [sys.executable, str(SCRIPTS / 'drive_checks.py'), 'reads', *check_common,
                   '--allowlist', str(allowlist), '--acceptance', str(crc), '--out', str(reads)], [reads / 'results.json'],
                   lambda: cohort_complete(reads / 'results.json', [e['serial'] for e in c['allowlist']]), allow_failure=True)
        pool = self.out / 'pool'
        pool_command = [sys.executable, str(SCRIPTS / 'storage_pool.py'), '--module', module_path,
                        '--expected-tag', c['expected_tag'], '--expected-drives', str(c['expected_drives']),
                        '--allowlist', str(allowlist), '--out', str(pool), '--size-gib', str(c['pool_gib']), '--apply']
        self.stage('pool', pool_command, [pool / 'manifest.json'],
                   lambda: json.loads((pool / 'manifest.json').read_text())['status'] == 'mounted', recover_pool=True)
        verify_pool = [arg for arg in pool_command if arg != '--apply'] + ['--resume']
        if self.command(verify_pool, self.out / 'pool-verify.log'):
            raise RuntimeError('live pool UUID, serial, geometry or mount verification failed')
        groups = json.loads((pool / 'manifest.json').read_text())['groups']
        run = self.out / 'mooncake'
        wheel = c['wheel']
        command = [sys.executable, str(SCRIPTS / 'system_profile.py'), '--expected-tag', c['expected_tag'],
                   '--evidence', str(self.out / 'system-profile'), '--', 'bash', str(ROOT / 'mooncake-loadtest.sh'),
                   'single', '--expected-tag', c['expected_tag'], '--expected-drives', str(c['expected_drives']),
                   '--l3-drives', str(len(c['allowlist'])), '--protocol', c['protocol'], '--profile', c['profile'],
                   '--wheel', wheel['flavor'], '--mooncake-version', wheel['version'], '--wheel-sha256', wheel['sha256'],
                   '--wheel-file' if wheel.get('file') else '--wheel-url', wheel.get('file') or wheel['url'],
                   '--disk-dirs', ','.join(g['mount'] for g in groups), '--segment-gb', str(c['segment_gib']),
                   '--disk-cap-gb', str(c['disk_cap_gib']), '--workers', str(c['workers']), '--batch', str(c['batch']),
                   '--skip', 'nvme', '--out', str(run)]
        if c['nics']:
            command += ['--nics', ','.join(c['nics'])]
        self.stage('mooncake', command, [run / 'report.json', self.out / 'system-profile/result.json'],
                   lambda: profile_complete(self.out / 'system-profile/result.json', run / 'report.json'), allow_failure=True)
        profile = json.loads((self.out / 'system-profile/result.json').read_text())
        if profile['restore_errors']:
            raise RuntimeError('host profile did not restore; stop for review')
        boot_read = self.out / 'boot-reads'
        self.stage('boot-reads', [sys.executable, str(SCRIPTS / 'drive_checks.py'), 'reads', *check_common,
                   '--boot-serial', c['boot_serial'], '--out', str(boot_read)], [boot_read / 'results.json'],
                   lambda: cohort_complete(boot_read / 'results.json', [c['boot_serial']]), allow_failure=True)
        boot_full = self.out / 'boot-full-read'
        self.stage('boot-full-read', [sys.executable, module_path, 'os-read', '--expected-tag', c['expected_tag'],
                   '--device', boot['device'], '--serial', boot['serial'], '--out', str(boot_full)],
                   [boot_full / 'result.json'], lambda: verified_crc(boot_full, [boot], self.module, writable=False))
        tests = self.out / 'selftests'
        self.stage('selftests', [sys.executable, str(SCRIPTS / 'drive_checks.py'), 'selftest', *check_common,
                   '--out', str(tests)], [tests / 'results.json'],
                   lambda: cohort_complete(tests / 'results.json', [e['serial'] for e in c['allowlist']] + [c['boot_serial']]),
                   allow_failure=True)
        self.finish()

    def finish(self):
        c = self.config
        reads = json.loads((self.out / 'data-reads/results.json').read_text())['drives']
        tests = json.loads((self.out / 'selftests/results.json').read_text())['drives']
        tested = {r['serial']: r for r in reads}
        data_ok = set(tested) == {e['serial'] for e in c['allowlist']} and all(
            r.get('status') == 'MEASURED' and r.get('health_clean') and len(r.get('speeds_GB_s', [])) == 3
            and min(r['speeds_GB_s']) >= c['minimum_read_gbps'] for r in reads)
        expected_serials = {e['serial'] for e in c['allowlist']} | {c['boot_serial']}
        health_ok = len(tests) == c['expected_drives'] and {r['serial'] for r in tests} == expected_serials and all(r.get('status') == 'PASS' and r.get('health_clean') for r in tests)
        boot_rows = json.loads((self.out / 'boot-reads/results.json').read_text())['drives']
        boot_ok = len(boot_rows) == 1 and boot_rows[0]['serial'] == c['boot_serial'] and boot_rows[0].get('health_clean') and boot_rows[0].get('status') == 'MEASURED'
        boot_speed_ok = bool(boot_ok and len(boot_rows[0].get('speeds_GB_s', [])) == 3
                             and min(boot_rows[0]['speeds_GB_s']) >= c['minimum_read_gbps'])
        mooncake = mooncake_verdict(self.out / 'mooncake')
        info = json.loads((self.out / 'mooncake/server/info.json').read_text())
        marker = f"Starting periodic task with interval: {c['offload_heartbeat_seconds']}s"
        logs = [self.out / f"mooncake/server/store/leg-{lane['idx']}.log" for lane in info['lanes']]
        mooncake['offload_interval_verified'] = bool(logs) and all(
            path.is_file() and marker in path.read_text(errors='replace') for path in logs)
        mooncake['passed'] = mooncake['passed'] and mooncake['offload_interval_verified']
        acceptance = dict(data_full_crc=verified_crc(self.out / 'data-crc', c['allowlist'], self.module),
                          data_read_speed=data_ok, boot_read_health=bool(boot_ok), boot_read_speed=boot_speed_ok, controller_short_tests=health_ok, mooncake=mooncake,
                          boot_scope='FULL_READ_ONLY; no destructive boot write qualification',
                          remote_rdma='NOT_QUALIFIED_SINGLE_HOST', require_rdma=c['require_rdma'])
        acceptance['acceptance_ready'] = bool(acceptance['data_full_crc'] and data_ok and health_ok
                                              and boot_ok and boot_speed_ok and mooncake['passed'] and not c['require_rdma'])
        save(self.out / 'acceptance.json', acceptance)
        lines = ['# Storage qualification', '', f"Acceptance ready: **{acceptance['acceptance_ready']}**", '',
                 'Boot coverage is read-only. Remote RDMA is not qualified by a same-host run.', '',
                 '| Serial | Read 1 GB/s | Read 2 GB/s | Read 3 GB/s | Critical / media / wear% / spare% | Short test |',
                 '|---|---:|---:|---:|---|---|']
        for row in reads + json.loads((self.out / 'boot-reads/results.json').read_text())['drives']:
            speeds = row.get('speeds_GB_s', [])
            values = [f'{v:.3f}' for v in speeds] + ['UNKNOWN'] * (3 - len(speeds))
            health = next((r['status'] for r in tests if r['serial'] == row['serial']), 'UNKNOWN')
            observed = ' / '.join(str(row.get(key, 'UNKNOWN')) for key in
                                  ('critical_warning', 'media_errors', 'percent_used', 'avail_spare'))
            lines.append(f"| {row['serial']} | {' | '.join(values)} | {observed} | {health} |")
        (self.out / 'report.md').write_text('\n'.join(lines) + '\n')
        self.state.update(status='complete' if acceptance['acceptance_ready'] else 'complete_not_accepted', current_stage=None)
        self.persist()

    def cleanup(self):
        if self.state['status'] not in ('complete', 'complete_not_accepted'):
            raise RuntimeError('collect and complete qualification before cleanup')
        self.prepare()
        c = self.config
        rc = self.command([sys.executable, str(SCRIPTS / 'storage_pool.py'), '--module', str(self.runtime / 'lib/drive_acceptance.py'),
                           '--expected-tag', c['expected_tag'], '--expected-drives', str(c['expected_drives']),
                           '--allowlist', str(self.out / 'allowlist.json'), '--out', str(self.out / 'pool'),
                           '--size-gib', str(c['pool_gib']), '--resume', '--cleanup', '--apply'], self.out / 'cleanup.log')
        if rc:
            raise RuntimeError('scoped cleanup failed; preserve ownership evidence')
        self.state['cleanup'] = 'complete'
        self.persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--inventory-only', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--erase-data', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if platform.system() != 'Linux' or os.geteuid() != 0:
        parser.error('node qualification requires Linux root')
    if args.apply != args.erase_data and not args.cleanup:
        parser.error('destructive qualification requires both --apply and --erase-data')
    if sum([args.inventory_only, args.plan, args.cleanup, args.apply]) > 1:
        parser.error('choose one operation')
    if args.cleanup and not args.resume:
        parser.error('cleanup requires --resume')
    config = normalize_node(json.loads(args.config.read_text()), complete=not args.inventory_only)
    if Path('/sys/class/dmi/id/product_serial').read_text().strip() != config['expected_tag']:
        parser.error('DMI identity mismatch; no installation or device changes performed')
    lock = open('/run/lock/mooncake-qualification.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    workflow = Workflow(config, args.out, args.resume)
    try:
        if args.cleanup:
            workflow.cleanup()
        else:
            workflow.execute(args.inventory_only, plan=not args.apply and not args.inventory_only)
    except Exception as error:
        workflow.state.update(status='failed_preserve_for_review', error=f'{type(error).__name__}: {error}')
        workflow.persist()
        raise


if __name__ == '__main__':
    main()
