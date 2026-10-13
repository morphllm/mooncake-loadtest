import ctypes
import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = tempfile.TemporaryDirectory()
subprocess.run(['bash', '-c', 'source "$1"; mkdir -p "$LIB"; write_payload',
                'test', str(ROOT / 'mooncake-loadtest.sh')],
               env={**__import__('os').environ, 'MCL_WORK': PAYLOAD.name}, check=True)


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(PAYLOAD.name) / 'lib' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bench = module('bench')


class Store:
    instances = []
    refusal = False
    incomplete = False

    def __init__(self):
        self.data = {}
        self.put_calls = 0
        self.closed = False
        self.instances.append(self)

    def setup(self, *args): return 0
    def register_buffer(self, *args): return 0
    def close(self): self.closed = True

    def batch_put_from(self, names, pointers, sizes):
        self.put_calls += 1
        if self.incomplete: return []
        if self.refusal: return [-200] * len(names)
        for name, ptr, size in zip(names, pointers, sizes):
            self.data[name] = ctypes.string_at(ptr, size)
        return [0] * len(names)

    def batch_get_into(self, names, pointers, sizes):
        results = []
        for name, ptr, size in zip(names, pointers, sizes):
            if name in self.data:
                ctypes.memmove(ptr, self.data[name], size)
                results.append(size)
            else:
                results.append(-704)
        return results


class RunnerTests(unittest.TestCase):
    def setUp(self):
        Store.refusal = Store.incomplete = False
        self.cfg = {'object_bytes': 8192, 'batch': 2, 'seed': 42, 'local_buffer': 65536,
                    'protocol': 'tcp', 'run_id': 'test'}
        self.lane = {'idx': 0, 'local_ip': '127.0.0.1', 'local_dev': '', 'master': 'local'}

    def run_worker(self, commands, ring=None):
        cmd, out = queue.Queue(), queue.Queue()
        for command in commands: cmd.put(command)
        cmd.put(None)
        fake = types.ModuleType('mooncake.store')
        fake.MooncakeDistributedStore = Store
        with patch.dict(sys.modules, {'mooncake.store': fake}):
            bench.worker(self.cfg, self.lane, 0, cmd, out, ring)
        return [out.get_nowait()[3] for _ in range(out.qsize())][1:]

    def command(self, op, lo=0, hi=4, deadline=0):
        return {'op': op, 'lo': lo, 'hi': hi, 'start': lo, 'step': 1,
                'cycle': False, 'deadline': deadline}

    def test_put_get_counts_verified_bytes_and_misses(self):
        put, get, miss = self.run_worker([self.command('put'), self.command('get'), self.command('get', 4, 6)])
        self.assertEqual(put['bytes'], 4 * 8192)
        self.assertEqual(get['bytes'], put['bytes'])
        self.assertEqual(get['mismatch'], 0)
        self.assertEqual(miss['bytes'], 0)
        self.assertEqual(miss['misses'], 2)
        self.assertEqual(miss['offered_bytes'], 2 * 8192)
        self.assertTrue(Store.instances[-1].closed)

    def test_refusal_stops_at_phase_deadline(self):
        Store.refusal = True
        started = time.monotonic()
        result, = self.run_worker([self.command('put', deadline=time.time() + .1)])
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result['bytes'], 0)
        self.assertEqual(result['errors'], {})
        self.assertGreater(result['deadline_refused'], 0)

    def test_incomplete_batch_is_error(self):
        Store.incomplete = True
        with self.assertRaisesRegex(RuntimeError, 'incomplete batch_put'):
            self.run_worker([self.command('put')])

    def test_shared_window_preserves_mixed_direction_rates(self):
        put, get = self.run_worker([self.command('put'), self.command('get')])
        for stat in (put, get):
            result = bench.merge([stat], 8192, 1, window=(10, 20))
            self.assertEqual(result['seconds'], 10)
            self.assertAlmostEqual(result['gbps'], stat['bytes'] / 10 / 1e9)
            self.assertEqual(result['window'], [10, 20])

    def test_worker_exit_breaks_collection(self):
        pool = bench.Bench.__new__(bench.Bench)
        pool.cfg = {'phase_timeout_s': .01}
        pool.res_q = queue.Queue()
        pool.procs = [types.SimpleNamespace(exitcode=1)]
        with self.assertRaisesRegex(RuntimeError, 'worker died'):
            list(pool.collect({(0, 0): {}}))

    def test_rolling_ring_contains_only_accepted_puts(self):
        import multiprocessing as mp
        ring = mp.Array('q', [-1] * 4)
        put = {**self.command('put'), 'publish': True, 'writer': 0, 'writers': 1}
        get = {**self.command('get'), 'rolling': True, 'deadline': time.time() + .3}
        inserted, read = self.run_worker([put, get], ring)
        self.assertEqual(set(bench.rolling_keys(ring, 0, 4)), {0, 1, 2, 3})
        self.assertGreater(read['bytes'], 0)
        self.assertEqual(read['misses'], 0)
        self.assertEqual(read['mismatch'], 0)
        Store.refusal = True
        empty = mp.Array('q', [-1] * 4)
        put['deadline'] = time.time() + .1
        self.run_worker([put], empty)
        self.assertEqual(bench.rolling_keys(empty, 0, 4), [])

    def test_cleanup_preserves_unrelated_matching_process(self):
        unrelated = subprocess.Popen(['bash', '-c', 'exec -a mooncake_master sleep 30'])
        owned = subprocess.Popen(['sleep', '30'])
        try:
            with tempfile.TemporaryDirectory() as directory:
                Path(directory, 'pids').write_text(f'{owned.pid} leg0\n')
                result = subprocess.run(['bash', '-c',
                    'source "$1"; S="$2"; SRV_PIDS=("$3"); server_cleanup',
                    'test', str(ROOT / 'mooncake-loadtest.sh'), directory, str(owned.pid)],
                    capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                owned.wait(timeout=1)
                self.assertIsNone(unrelated.poll())
        finally:
            for process in (owned, unrelated):
                if process.poll() is None: process.kill()
                process.wait()

    def test_managed_python_reuses_isolated_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory, 'bin/uv')
            runtime.parent.mkdir()
            runtime.write_text('#!/bin/sh\n[ "$UV_PYTHON_INSTALL_DIR" = "'+directory+'/python" ] || exit 8\ncase "$2" in install) exit 0 ;; find) printf "/isolated/python3.13\\n" ;; *) exit 9 ;; esac\n')
            runtime.chmod(0o755)
            completed = subprocess.run(['bash', '-c', 'source "$1"; managed_python',
                                        'test', str(ROOT / 'mooncake-loadtest.sh')],
                                       env={**__import__('os').environ, 'MCL_WORK': directory},
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), '/isolated/python3.13')

    def test_report_renders_without_successful_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'server').mkdir()
            Path(directory, 'client').mkdir()
            Path(directory, 'meta.json').write_text(json.dumps({'mode': 'single'}))
            completed = subprocess.run([sys.executable, str(Path(PAYLOAD.name) / 'lib/report.py'), directory],
                                       capture_output=True, text=True)
            self.assertIn(completed.returncode, (0, 1), completed.stderr)
            report = Path(directory, 'report.txt').read_text()
            self.assertIn('Observed payload bandwidth', report)
            self.assertTrue(Path(directory, 'report.json').exists())


if __name__ == '__main__': unittest.main()
