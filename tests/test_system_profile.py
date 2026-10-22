"""Host-profile lifecycle checks using fake sysfs/proc and resource limits."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import resource
import signal
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('profile_runner', Path(__file__).resolve().parents[1] / 'scripts/system_profile.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class HostGuardTests(unittest.TestCase):
    def test_os_root_and_identity_are_required(self):
        for system, uid, tag in [('Darwin', 0, 'expected'), ('Linux', 1000, 'expected'), ('Linux', 0, 'wrong')]:
            with self.subTest(system=system, uid=uid, tag=tag), \
                 patch.object(m.platform, 'system', return_value=system), \
                 patch.object(m.os, 'geteuid', return_value=uid), \
                 patch.object(Path, 'read_text', return_value=tag):
                with self.assertRaises(RuntimeError):
                    m.verify_host('expected')


class ProfileFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='morph-profile-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sys, self.proc = self.base/'sys', self.base/'proc'
        original_write = Path.write_text
        self.clamp = False
        def write(path, value, *a, **kw):
            if path.name == 'nr_hugepages':
                n = int(value)
                if self.clamp and n == m.PAGES_PER_NODE:
                    n -= 1
                original_write(path.with_name('free_hugepages'), str(n))
                value = str(n)
            return original_write(path, value, *a, **kw)
        files = {
            'sys/devices/system/cpu/cpufreq/policy0/scaling_governor': 'schedutil',
            'proc/sys/kernel/numa_balancing': '1',
            'proc/self/limits': 'fixture limits\n',
        }
        files.update({
            f'sys/devices/system/node/node{n}/hugepages/hugepages-2048kB/{key}': str(value)
            for n in (0, 1)
            for key, value in [('nr_hugepages', 4), ('free_hugepages', 4), ('surplus_hugepages', 0)]
        })
        for name, value in files.items():
            path = self.base/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.values = {resource.RLIMIT_MEMLOCK: (65536, 65536), resource.RLIMIT_NOFILE: (1024, 4096)}
        self.initial = self.values.copy()
        self.fail_limit = False
        def set_limit(key, value):
            if self.fail_limit and key == resource.RLIMIT_MEMLOCK and value[0] == resource.RLIM_INFINITY:
                raise ValueError('fixture denied unlimited memlock')
            self.values[key] = value
        self.addCleanup(patch.stopall)
        patch.object(m.resource, 'getrlimit', lambda key: self.values[key]).start()
        patch.object(m.resource, 'setrlimit', set_limit).start()
        patch.object(Path, 'write_text', write).start()
        self.profile = m.Profile(self.sys, self.proc)
        real_no_fio = m.no_fio
        patch.object(m, 'Profile', lambda: self.profile).start()
        patch.object(m, 'no_fio', lambda proc=None: real_no_fio(proc or self.proc)).start()
        self.no_fio = real_no_fio
        patch.object(m, 'verify_host', lambda expected: None).start()
        patch.dict(os.environ, {'MC_MMAP_ARENA_POOL_SIZE': ''}).start()

    def args(self, code='pass'):
        return argparse.Namespace(expected_tag='fixture', evidence=self.base/'evidence', command=[sys.executable, '-c', code])

    def restored(self):
        self.assertEqual(self.values, self.initial)
        for path, value in self.profile.original.items():
            self.assertEqual(path.read_text().strip(), value)

    def test_success_and_normal_group_cleanup(self):
        envfile = self.base/'child-env.json'
        args = self.args(f'import json,os;json.dump(dict(os.environ),open({str(envfile)!r},"w"))')
        self.assertEqual(m.run(args), 0)
        self.restored()
        child_env = json.loads(envfile.read_text())
        for key, value in m.ENVIRONMENT.items():
            self.assertEqual(child_env[key], value)
        applied = json.loads((args.evidence/'applied.json').read_text())
        self.assertEqual(applied['memlock'], [resource.RLIM_INFINITY] * 2)
        self.assertEqual(applied['nofile'], [65536, 65536])
        self.assertEqual(applied['hugepages_2MiB']['node0']['nr_hugepages'], 16384)
        self.assertIn('proc_self_limits', applied)
        self.assertTrue((args.evidence/'runtime.jsonl').exists())
        self.assertEqual(json.loads((args.evidence/'result.json').read_text())['returncode'], 0)

    def test_child_nonzero_restores(self):
        self.assertEqual(m.run(self.args('raise SystemExit(7)')), 7)
        self.restored()

    def test_partial_allocation_restores_without_child(self):
        self.clamp = True
        args = self.args('raise SystemExit(99)')
        self.assertEqual(m.run(args), 1)
        self.restored()
        self.assertIsNone(json.loads((args.evidence/'result.json').read_text())['command_returncode'])

    def test_limit_failure_restores_without_knob_changes(self):
        self.fail_limit = True
        self.assertEqual(m.run(self.args()), 1)
        self.restored()
        self.assertEqual(self.profile.changed, [])

    def test_existing_evidence_refused(self):
        args = self.args()
        args.evidence.mkdir()
        with self.assertRaises(FileExistsError):
            m.run(args)
        self.restored()
        self.assertEqual(self.profile.changed, [])

    def test_signal_terminates_child_and_restores(self):
        timer = threading.Timer(.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            self.assertEqual(m.run(self.args('import time;time.sleep(60)')), 143)
        finally:
            timer.cancel()
            timer.join()
        self.restored()
        self.assertEqual(json.loads((self.base/'evidence/result.json').read_text())['command_returncode'], -15)

    def test_fio_guard_and_arena_guard(self):
        d = self.proc/'123'
        d.mkdir()
        (d/'comm').write_text('fio')
        (d/'exe').symlink_to('/usr/bin/fio')
        with self.assertRaisesRegex(RuntimeError, 'fio'):
            self.no_fio(self.proc)
        with patch.dict(os.environ, {'MC_MMAP_ARENA_POOL_SIZE': '4GiB'}):
            with self.assertRaisesRegex(RuntimeError, 'MC_MMAP'):
                m.run(self.args())
        self.assertEqual(self.profile.changed, [])

    def test_sampler_records_descendants_hugetlb_and_limits(self):
        for pid, ppid in ((100, 1), (101, 100), (200, 1)):
            p = self.proc/str(pid)
            p.mkdir()
            (p/'stat').write_text(f'{pid} (fixture task) S {ppid} 0 0')
            (p/'cmdline').write_bytes(b'fixture\0child\0')
            (p/'status').write_text('Name:\tfixture\nCpus_allowed_list:\t0-3\nMems_allowed_list:\t0-1\n')
            (p/'smaps_rollup').write_text('Rss: 12 kB\nPrivate_Hugetlb: 2048 kB\nShared_Hugetlb: 4096 kB\nAnonHugePages: 0 kB\n')
            (p/'limits').write_text('Max locked memory unlimited unlimited bytes\nMax open files 65536 65536 files\n')
        records = m.sample_processes(100, self.proc)
        self.assertEqual({x['pid'] for x in records}, {100, 101})
        self.assertEqual(records[1]['smaps_rollup']['Private_Hugetlb'], '2048 kB')
        self.assertIn('Max locked memory', records[1]['limits'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
