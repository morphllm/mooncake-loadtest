"""Coordinator contract tests use fake SSH and temporary files, never real nodes."""
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fleet


def node(name='storage1'):
    return dict(name=name, ssh=name, sudo=True, expected_tag='TAG', run_id='fixture', expected_drives=3,
                boot_serial='BOOT', allowlist=[dict(device=f'/dev/nvme{i}n1', serial=f'DATA{i}', bytes=1000) for i in range(2)],
                wheel=dict(flavor='non-cuda', version='1.0', file='/private/wheel.whl', sha256='a'*64))


def snapshot():
    return dict(commit='b'*40, dirty=False, files={'mooncake-loadtest.sh': b'canonical', 'scripts/qualify_node.py': b'node'})


class FleetTests(unittest.TestCase):
    def config(self, value, complete=True):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fleet.json'; p.write_text(json.dumps(value))
            return fleet.load_config(p, complete)

    def test_schema_and_reviewed_allowlist(self):
        value = dict(schema=1, run_id='fixture', nodes=[node()])
        self.assertEqual(self.config(value)['nodes'][0]['expected_drives'], 3)
        for run_id in ['../escape', '-option', 'two words']:
            with self.assertRaises(ValueError): self.config(dict(value, run_id=run_id))
        for target in ['-oProxyCommand=bad', 'host;touch', 'user@host $(bad)']:
            with self.assertRaises(ValueError): self.config(dict(value, nodes=[dict(node(), ssh=target)]))
        with self.assertRaises(ValueError): self.config(dict(value, nodes=[dict(node(), boot_serial='DATA0')]))

    def test_inventory_accepts_no_allowlist_but_run_does_not(self):
        value = dict(schema=1, run_id='fixture', nodes=[dict(name='n1', ssh='n1', sudo=True, expected_tag='TAG')])
        self.assertEqual(self.config(value, complete=False)['nodes'][0]['allowlist'], [])
        with self.assertRaises(ValueError): self.config(value)

    def test_parallel_node_failure_does_not_cancel_peer(self):
        barrier = threading.Barrier(2)
        def operation(n):
            barrier.wait(timeout=2)
            if n['name'] == 'bad': raise RuntimeError('one node failed')
            return 'peer completed'
        result = fleet.parallel([node('bad'), node('good')], 2, operation)
        self.assertFalse(result[0]['ok']); self.assertTrue(result[1]['ok'])
        self.assertEqual(result[1]['result'], 'peer completed')

    def test_ssh_quotes_argv_without_hostkey_or_auth_bypass(self):
        argv = ['python3', '-c', 'print("$(touch /tmp/unsafe); quote")', 'file with spaces']
        with patch.object(fleet.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'ok', stderr=b'')) as execute:
            fleet.SSH(node()).call(argv)
        command = execute.call_args.args[0]
        self.assertEqual(shlex.split(command[-1]), ['sudo', '-n', *argv])
        self.assertIn('BatchMode=yes', command); self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertNotIn('StrictHostKeyChecking=no', command)

    def test_inventory_plan_and_run_have_distinct_directories(self):
        paths = {fleet.remote_path(node(), mode) for mode in ['inventory', 'plan', 'run']}
        self.assertEqual(len(paths), 3)
        self.assertEqual(fleet.remote_path(node(), 'cleanup'), fleet.remote_path(node(), 'run'))
        self.assertIn('--inventory-only', fleet.node_command(node(), 'inventory'))
        self.assertIn('--plan', fleet.node_command(node(), 'plan'))
        self.assertEqual(fleet.node_command(node(), 'cleanup')[-2:], ['--cleanup', '--resume'])

    def test_bundle_transfer_and_resume_validate_every_file(self):
        payload, checksum, manifest = fleet.bundle(snapshot(), node())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'owned'
            def stage(resume, data=payload, digest=checksum):
                return subprocess.run([sys.executable, '-c', fleet.STAGE, str(root), digest, str(int(resume))],
                                      input=data, capture_output=True)
            self.assertEqual(stage(False).returncode, 0)
            self.assertEqual(stage(True).returncode, 0)
            self.assertNotEqual(stage(False).returncode, 0)
            (root / 'scripts/qualify_node.py').write_text('changed')
            self.assertNotEqual(stage(True).returncode, 0)
            self.assertNotEqual(stage(True, digest='0'*64).returncode, 0)
            self.assertEqual(manifest['files']['mooncake-loadtest.sh'], fleet.sha(b'canonical'))

    def test_run_uses_persistent_owned_unit_with_limits(self):
        class FakeSSH:
            def __init__(self): self.calls = []
            def call(self, args, **kwargs):
                self.calls.append(args)
                if fleet.STATUS in args: return json.dumps(dict(state=None, missing_state=True, unit={}, unit_query_returncode=1))
                return 'launched'
        fake = FakeSSH(); result = fleet.launch(fake, node(), 'run', resume=True)
        cmd = fake.calls[-1]
        self.assertEqual(result['status'], 'launched')
        self.assertIn('--property=LimitMEMLOCK=infinity', cmd)
        self.assertIn('--property=LimitNOFILE=65536', cmd)
        self.assertIn('--erase-data', cmd); self.assertIn('--resume', cmd)
        self.assertFalse(any(c[0] == 'systemctl' for c in fake.calls))

    def test_collect_excludes_sibling_runtime_and_pins_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'artifacts').mkdir(); (root/'runtime').mkdir()
            (root/'artifacts/report.md').write_text('evidence')
            (root/'runtime/private.whl').write_text('private')
            p = subprocess.run([sys.executable, '-c', fleet.COLLECT, str(root)], capture_output=True, check=True)
            with tarfile.open(fileobj=io.BytesIO(p.stdout)) as archive:
                self.assertEqual(set(archive.getnames()), {'artifacts/report.md', 'ARTIFACT_SHA256.json'})
                hashes = json.load(archive.extractfile('ARTIFACT_SHA256.json'))
                self.assertEqual(hashes['artifacts/report.md'], fleet.sha(b'evidence'))

    def test_dirty_source_is_rejected_before_staging(self):
        with patch.object(fleet.subprocess, 'run', return_value=types.SimpleNamespace(stdout=' M mooncake-loadtest.sh')):
            with self.assertRaisesRegex(RuntimeError, 'uncommitted'):
                fleet.source_snapshot()


if __name__ == '__main__': unittest.main()
