"""Validate emitted jobs with actual fio and prove raw read-only enforcement."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'mooncake-loadtest.sh'


class FioTests(unittest.TestCase):
    def setUp(self):
        self.fio = shutil.which('fio')
        self.assertIsNotNone(self.fio, 'install the fio benchmark (CI installs it)')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / 'scratch.bin'
        self.target.write_bytes(bytes(range(256)) * (8 << 12))

    def emit(self, function, *args):
        completed = subprocess.run(['bash', '-c', 'source "$1"; shift; "$@"',
                                    'test', str(SCRIPT), function, *map(str, args)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        job = self.root / 'job.fio'
        job.write_text(completed.stdout)
        return job

    def test_raw_jobs_parse_and_read_only_guard_preserves_data(self):
        before = hashlib.sha256(self.target.read_bytes()).digest()
        for mode in ('read', 'randread'):
            job = self.emit('raw_fio_job', 'psync', '4096', mode, '1', self.target)
            parsed = subprocess.run([self.fio, '--readonly', '--parse-only', str(job)],
                                    capture_output=True, text=True)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
        output = self.root / 'result.json'
        result = subprocess.run([self.fio, '--readonly', '--output-format=json', f'--output={output}', str(job)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        measured = json.loads(output.read_text())['jobs'][0]
        self.assertGreater(measured['read']['io_bytes'], 0)
        self.assertEqual(measured['write']['io_bytes'], 0)
        unsafe = self.root / 'write.fio'
        unsafe.write_text(f'[scratch]\nfilename={self.target}\nrw=write\nbs=4096\nsize=4096\n')
        rejected = subprocess.run([self.fio, '--readonly', str(unsafe)], capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).digest(), before)

    def test_raw_generator_rejects_writes(self):
        result = subprocess.run(['bash', '-c', 'source "$1"; raw_fio_job psync 4096 write 1 "$2"',
                                 'test', str(SCRIPT), str(self.target)], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_full_crc_job_writes_and_verifies_bulk_and_tail(self):
        from test_runner import module
        accept = module('drive_acceptance')
        size = (2 << 20) + 4096
        # psync lets the data-integrity test run in CI containers whose seccomp
        # policy disallows io_uring; deployed jobs retain io_uring.
        job = self.root / 'verify.fio'
        job.write_text(accept.fio_job(self.root / 'crc.bin', size, True, regular=True)
                       .replace('ioengine=io_uring', 'ioengine=psync').replace('iodepth=32', 'iodepth=1'))
        parsed = subprocess.run([self.fio, '--parse-only', str(job)], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        output = self.root / 'verify.json'
        result = subprocess.run([self.fio, '--output-format=json', f'--output={output}', str(job)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        self.assertTrue(accept.fio_pass(json.loads(output.read_text()), size, True))

    def test_all_filesystem_jobs_parse(self):
        for mode in ('bufwrite', 'dirwrite', 'objread'):
            job = self.emit('fs_fio_job', mode, self.root, 'libaio', '128', '21233664', '1')
            parsed = subprocess.run([self.fio, '--parse-only', str(job)], capture_output=True, text=True)
            self.assertEqual(parsed.returncode, 0, f'{mode}: {parsed.stderr}')


if __name__ == '__main__': unittest.main()
