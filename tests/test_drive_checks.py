"""Pure drive-check fixtures: no commands, sysfs, or hardware are accessed."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
import types
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('drive_checks', Path(__file__).resolve().parents[1] / 'scripts/drive_checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
accept = types.ModuleType('canonical_drive_acceptance')
launcher = (Path(__file__).resolve().parents[1] / 'mooncake-loadtest.sh').read_text()
payload = launcher.split("<<'PYDRIVE'\n", 1)[1].split('\nPYDRIVE\n', 1)[0]
exec(compile(payload, 'canonical-drive-acceptance-fixture', 'exec'), accept.__dict__)


def fixture():
    def node(name, kind, serial='', mounts=None, children=None):
        return dict(name=name, kname=name, path='/dev/' + name, type=kind, serial=serial,
                    size=1048576 + 4096, ro=False, mountpoints=mounts or [], children=children or [])
    tree = {'blockdevices': [node('nvme0n1', 'disk', 'BOOT', children=[
        node('nvme0n1p1', 'part', mounts=['/'])]), node('nvme1n1', 'disk', 'DATA')]}
    nodes, edges = accept.flatten(tree)
    return dict(service_tag='EXPECTED', nodes=nodes, lsblk=tree,
                protected=accept.protection(nodes, edges, set(), {}, {})), edges

SMART = dict(critical_warning=0, media_errors=0, num_err_log_entries=7,
             percent_used=3, avail_spare=100, spare_thresh=10, temperature=300)


def result(value, code=0):
    return dict(returncode=code, stdout=json.dumps(value), stderr='')


class FakeCommands:
    def __init__(self):
        self.stop = threading.Event()
        self.signum = None
        self.calls = []
        self.started = set()
        self.fail_fio = False
        self.preexisting = False
        self.interrupt = False

    def idle(self):
        pass

    def run(self, argv, timeout=30):
        self.calls.append(argv)
        if 'fio' in argv:
            if self.fail_fio:
                return result({}, 1)
            path = Path(next(x.partition('=')[2] for x in argv if x.startswith('--output=')))
            checks.save(path, {'jobs': [dict(error=0, read=dict(io_bytes=180000000000, runtime=30000,
                clat_ns={'percentile': {'99.000000': 1000000}}), write={'io_bytes': 0}, trim={'io_bytes': 0})]})
            time.sleep(.003)  # Let the fake SMART sampler run; no real I/O is performed.
            return result({})
        if argv[1] == 'smart-log':
            return result(SMART)
        if argv[1] == 'self-test-log':
            return result({'Current Device Self-Test Operation': int(self.preexisting),
                           'List of Valid Reports': [{'Self test result': 0, 'Self test code': 1,
                                                      'Power on hours': 2 if argv[2] in self.started else 1}]})
        if argv[1] == 'device-self-test':
            if argv[-1] != '--self-test-code=1':
                raise AssertionError('unexpected self-test operation')
            self.started.add(argv[2])
            if self.interrupt:
                self.signum = signal.SIGTERM
                self.stop.set()
            return result({})
        raise AssertionError(argv)


class DriveChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inv, _ = fixture()
        self.data = self.inv['nodes']['nvme1n1']
        self.allowed = [dict(device=self.data['path'], serial='DATA', bytes=self.data['size'])]
        self.args = argparse.Namespace(mode='reads', module=self.root/'module.py', expected_tag='EXPECTED',
            expected_drives=2, inventory=self.root/'inventory.json', out=self.root/'out',
            allowlist=self.root/'allowlist.json', boot_serial=None, acceptance=self.root/'acceptance', precondition=None)
        checks.save(self.args.inventory, self.inv)
        checks.save(self.args.allowlist, self.allowed)
        self.commands = FakeCommands()

    def proof(self):
        directory = self.args.acceptance/'DATA'
        directory.mkdir(parents=True)
        checks.save(self.args.acceptance/'inventory.json', self.inv)
        row = dict(device=self.data['path'], serial='DATA', bytes=self.data['size'], status='FULL_BYTES_VERIFIED',
                   test='FULL_WRITE_CRC32C_READBACK', fio_returncode=0, health={'status': 'NO_NEW_RECORDED_FAULT'})
        checks.save(self.args.acceptance/'results.json', [row])
        checks.save(directory/'result.json', row)
        checks.save(directory/'fio.json', {'jobs': [dict(error=0, read={'io_bytes': self.data['size']},
                                                       write={'io_bytes': self.data['size']})]})
        (directory/'full.fio').write_text(accept.fio_job(self.data['path'], self.data['size'], True))
        return directory

    def health(self, device, directory, label):
        values = dict(smart=result(SMART), identify=result({'oacs': 607}), errors=result({}))
        checks.save(directory/f'{label}.json', values)
        return values

    def test_dynamic_inventory_and_exact_capacity_guard(self):
        self.assertEqual(len(checks.validate_inventory(accept, self.inv, self.inv, 'EXPECTED', 2)), 2)
        bad = copy.deepcopy(self.inv)
        bad['nodes']['nvme1n1']['size'] += 4096
        for tag, inv, count in [('WRONG', self.inv, 2), ('EXPECTED', bad, 2), ('EXPECTED', self.inv, 3)]:
            with self.subTest(tag=tag, count=count), self.assertRaises(RuntimeError):
                checks.validate_inventory(accept, self.inv, inv, tag, count)

    def test_boot_requires_root_ancestry_and_data_rejects_boot(self):
        self.args.boot_serial = 'BOOT'
        self.args.allowlist = self.args.acceptance = None
        self.assertEqual(checks.select(accept, self.inv, self.args)[0]['serial'], 'BOOT')
        self.args.boot_serial = 'DATA'
        with self.assertRaises(RuntimeError):
            checks.select(accept, self.inv, self.args)
        self.args.boot_serial = None
        self.args.allowlist = self.root/'boot-allowlist.json'
        boot = self.inv['nodes']['nvme0n1']
        checks.save(self.args.allowlist, [dict(device=boot['path'], serial='BOOT', bytes=boot['size'])])
        with self.assertRaises(RuntimeError):
            checks.select(accept, self.inv, self.args)

    def test_full_crc_proof_requires_raw_fio_and_exact_job(self):
        directory = self.proof()
        extents, scope = checks.initialized_extents(accept, self.args, [self.data])
        self.assertEqual(extents['DATA'], (0, self.data['size']))
        self.assertIn('FULL_CAPACITY_CRC_PROVEN', scope)
        raw = checks.load(directory/'fio.json')
        raw['jobs'][0]['read']['io_bytes'] -= 4096
        checks.save(directory/'fio.json', raw)
        with self.assertRaisesRegex(RuntimeError, 'proof invalid'):
            checks.initialized_extents(accept, self.args, [self.data])

    def test_status_only_or_uninitialized_proof_is_rejected(self):
        directory = self.proof()
        (directory/'full.fio').write_text('rw=read\ntime_based=1\n')
        with self.assertRaises(RuntimeError):
            checks.initialized_extents(accept, self.args, [self.data])
        self.args.acceptance = None
        with self.assertRaises(RuntimeError):
            checks.initialized_extents(accept, self.args, [self.data])

    def test_bounded_manifest_is_explicit_and_cannot_claim_full_surface(self):
        self.args.acceptance = None
        self.args.precondition = self.root/'bounded.json'
        row = dict(device=self.data['path'], serial='DATA', namespace_bytes=self.data['size'],
                   discard_proven=True, discard_bytes=self.data['size'], crc_extent_verified=True,
                   initialized_offset=0, initialized_bytes=1048576, write_bytes=1048576, read_bytes=1048576,
                   full_surface_verified=False, health_delta={'status': 'NO_NEW_RECORDED_FAULT'})
        manifest = dict(service_tag='EXPECTED', status='COMPLETE_BOUNDED_EXTENTS', full_surface_verified=False,
                        initialized_offset=0, initialized_bytes_per_drive=1048576, results=[row])
        checks.save(self.args.precondition, manifest)
        extents, scope = checks.initialized_extents(accept, self.args, [self.data])
        self.assertEqual(extents['DATA'], (0, 1048576))
        self.assertIn('NOT_FULL_SURFACE', scope)
        manifest['full_surface_verified'] = True
        checks.save(self.args.precondition, manifest)
        with self.assertRaises(RuntimeError):
            checks.initialized_extents(accept, self.args, [self.data])

    def read(self):
        self.args.out.mkdir()
        governor = self.root/'governor'
        governor.write_text('schedutil')
        with patch.object(checks, 'locality', return_value=(0, 2, governor)), \
                patch.object(accept, 'health', side_effect=self.health):
            row = checks.read_one(accept, self.args, self.data, (0, 1048576), lambda: None,
                                  self.commands, 'FIXTURE_BOUNDED_NOT_FULL_SURFACE')
        self.assertEqual(governor.read_text().strip(), 'schedutil')
        return row

    def test_three_direct_reads_numeric_health_and_governor_restore(self):
        row = self.read()
        self.assertEqual(row['status'], 'MEASURED')
        self.assertEqual(row['speeds_GB_s'], [6.0, 6.0, 6.0])
        self.assertEqual(row['percent_used'], 3)
        self.assertEqual(row['num_err_log_entries_delta'], 0)
        jobs = [x for x in self.commands.calls if 'fio' in x]
        self.assertEqual(len(jobs), 3)
        for argv in jobs:
            for flag in ('--readonly', '--direct=1', '--runtime=30', '--ramp_time=5', '--bs=128k', '--iodepth=32'):
                self.assertIn(flag, argv)

    def test_read_failure_still_restores_governor(self):
        self.commands.fail_fio = True
        self.assertNotEqual(self.read()['status'], 'MEASURED')

    def test_selftest_all_drives_uses_only_short_test_requests(self):
        self.args.mode = 'selftest'
        with patch.object(accept, 'inventory', return_value=self.inv), patch.object(accept, 'health', side_effect=self.health):
            self.assertEqual(checks.execute(self.args, accept, 'fixture-source-sha', self.commands), 0)
        rows = checks.load(self.args.out/'results.json')['drives']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['status'] == 'PASS' for r in rows))
        self.assertEqual(len(self.commands.started), 2)
        self.assertFalse(any('--wait' in x for x in self.commands.calls))

    def test_preexisting_selftest_prevents_every_start(self):
        self.args.mode = 'selftest'
        self.commands.preexisting = True
        with patch.object(accept, 'inventory', return_value=self.inv), patch.object(accept, 'health', side_effect=self.health):
            self.assertEqual(checks.execute(self.args, accept, 'fixture', self.commands), 1)
        self.assertEqual(self.commands.started, set())

    def test_interrupt_leaves_started_hardware_test_running(self):
        self.args.mode = 'selftest'
        self.commands.interrupt = True
        with patch.object(accept, 'inventory', return_value=self.inv), patch.object(accept, 'health', side_effect=self.health):
            self.assertEqual(checks.execute(self.args, accept, 'fixture', self.commands), 143)
        self.assertEqual(len(self.commands.started), 1)
        requests = [x[-1] for x in self.commands.calls if x[1] == 'device-self-test']
        self.assertEqual(requests, ['--self-test-code=1'])
        self.assertEqual(checks.load(self.args.out/'results.json')['metadata']['status'], 'INCOMPLETE_OR_FAILED')

    def test_stale_report_failed_report_and_wrong_code_are_not_passes(self):
        old = {'Current Device Self-Test Operation': 0,
               'List of Valid Reports': [{'Self test result': 0, 'Self test code': 1}]}
        self.assertEqual(checks.selftest_verdict(old, old, False), 'UNKNOWN_NO_NEW_REPORT')
        new = copy.deepcopy(old)
        new['List of Valid Reports'][0]['Self test result'] = 5
        self.assertEqual(checks.selftest_verdict(new, old, True), 'FAILED_SELFTEST')
        new['List of Valid Reports'][0].update({'Self test result': 0, 'Self test code': 2})
        self.assertEqual(checks.selftest_verdict(new, old, True), 'UNKNOWN_WRONG_TEST_CODE')

    def test_module_provenance_is_of_executed_source(self):
        path = self.root/'module.py'
        source = b'value = 41\n'
        path.write_bytes(source)
        loaded, digest = checks.load_module(path)
        self.assertEqual(loaded.value, 41)
        self.assertEqual(digest, hashlib.sha256(source).hexdigest())


if __name__ == '__main__':
    unittest.main()
