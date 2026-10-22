"""One-shot setup invariants, without touching block devices."""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from test_runner import module, ROOT

probe = module('probe')


class SetupTests(unittest.TestCase):
    def test_every_explicit_disk_directory_gets_a_store_leg(self):
        with tempfile.TemporaryDirectory() as root:
            lanes = Path(root, 'lanes.json')
            lanes.write_text(json.dumps([dict(idx=0, dev='mlx5_0', numa=0)]))
            dirs = [str(Path(root, str(i))) for i in range(24)]
            args = types.SimpleNamespace(lanes=lanes, dirs=','.join(dirs), root=root)
            with patch.object(probe, 'fs_of', return_value=dict(source='/dev/nvme0n1', mount=root, fstype='xfs')), patch.object(probe, 'nvme_under', return_value=['nvme0n1']), contextlib.redirect_stdout(io.StringIO()) as output:
                probe.cmd_diskdirs(args)
            results = json.loads(output.getvalue())
            self.assertEqual([r['base'] for r in results], dirs)
            self.assertEqual([l['idx'] for l in json.loads(lanes.read_text())], list(range(24)))
            self.assertTrue(all(Path(r['dir']).is_dir() for r in results))

    def test_node_zero_survives_shell_field_lookup_and_pinning(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'lanes.json').write_text('[{"idx":0,"numa":0,"local_numa":0}]')
            result = subprocess.run(['bash', '-c', 'source "$1"; PY=python3; S="$2"; C="$2"; numactl() { :; }; node=$(lane_field 0 numa); numa_pin "$node"; cl_field 0 local_numa', 'test', str(ROOT / 'mooncake-loadtest.sh'), root], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines(), ['numactl -N 0 -m 0', '0'])

    def test_tcp_lane_uses_all_backing_disks_numa(self):
        lane = dict(idx=0, dev='', numa=1)
        with patch.object(probe, 'nvme_sysdev', side_effect=lambda ns: ns), patch.object(probe, 'rd', return_value='0'):
            probe.bind_disk_numa(lane, dict(nvme=['nvme0n1', 'nvme1n1']))
        self.assertEqual((lane['numa'], lane['disk_numa'], lane['numa_source']), (0, 0, 'disk'))

    def test_mixed_or_unknown_backing_is_not_called_local(self):
        for nodes in [('0', '1'), ('0', '-1')]:
            lane = dict(idx=0, dev='', numa=1)
            with patch.object(probe, 'nvme_sysdev', side_effect=lambda ns: ns), patch.object(probe, 'rd', side_effect=nodes), contextlib.redirect_stderr(io.StringIO()) as warning:
                probe.bind_disk_numa(lane, dict(nvme=['nvme0n1', 'nvme1n1']))
            self.assertEqual((lane['numa'], lane['disk_numa']), (-1, -1))
            self.assertIn('leaving unbound', warning.getvalue())

    def test_rdma_device_preserves_nic_numa(self):
        lane = dict(idx=0, dev='mlx5_0', numa=1)
        with patch.object(probe, 'backing_numa', return_value=0):
            probe.bind_disk_numa(lane, dict(nvme=['nvme0n1']))
        self.assertEqual((lane['numa'], lane['disk_numa'], lane['numa_source']), (1, 0, 'nic'))

    def test_single_host_client_inherits_each_disk_lane_numa(self):
        with tempfile.TemporaryDirectory() as root:
            client = Path(root)
            lanes = [dict(idx=i, ip='127.0.0.1', dev='', netdev='lo', rate_gbit=None, numa=i) for i in range(2)]
            (client / 'info.json').write_text(json.dumps(dict(lanes=lanes)))
            subprocess.run(['bash', '-c', 'source "$1"; MODE=single; PY=python3; C="$2"; client_lanes', 'test', str(ROOT / 'mooncake-loadtest.sh'), root], check=True)
            self.assertEqual([l['local_numa'] for l in json.loads((client / 'lanes.json').read_text())], [0, 1])

    def test_duplicate_explicit_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            lanes = Path(root, 'lanes.json')
            lanes.write_text('[{"idx": 0}]')
            with self.assertRaisesRegex(SystemExit, 'duplicate'):
                probe.cmd_diskdirs(types.SimpleNamespace(lanes=lanes, dirs=f'{root},{root}', root=root))

    def test_reused_run_directory_cannot_append_old_samples(self):
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run(['bash', '-c', 'source "$1"; OUT="$2"; PY=python3; new_run', 'test', str(ROOT / 'mooncake-loadtest.sh'), root], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('already exists', result.stderr)
            self.assertFalse(Path(root, 'meta.json').exists())

    def test_wrong_server_rejected_before_setup(self):
        result = subprocess.run(['bash', '-c', 'source "$1"; EXPECTED_TAG=not-this-server; EXPECTED_DRIVES=""; verify_host', 'test', str(ROOT / 'mooncake-loadtest.sh')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('service tag mismatch', result.stderr)

    def test_master_help_accepts_gflags_exit_one_but_rejects_loader_error(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / 'mooncake').mkdir()
            (directory / 'mooncake/__init__.py').write_text('')
            (directory / 'mooncake/store.py').write_text('class MooncakeDistributedStore: pass\n')
            metadata = directory / 'mooncake_transfer_engine_non_cuda-1.0.dist-info'
            metadata.mkdir()
            (metadata / 'METADATA').write_text('Name: mooncake-transfer-engine-non-cuda\nVersion: 1.0\n')
            master = directory / 'mooncake_master'
            for output, expected in [('Flags from server.cc:', 0), ('error loading shared libraries', 2)]:
                master.write_text(f'#!/bin/sh\necho "{output}"\nexit 1\n')
                master.chmod(0o755)
                result = subprocess.run(['bash', '-c',
                    'source "$1"; PY="$2"; MC_DIR="$3"; WHEEL=non-cuda; MC_VERSION=1.0; validate_runtime',
                    'test', str(ROOT / 'mooncake-loadtest.sh'), sys.executable, root],
                    env={**os.environ, 'PYTHONPATH': root}, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_wheel_url_requires_https_named_artifact_and_checksum(self):
        for url, digest in [('http://example.com/test.whl', 'a' * 64),
                            ('https://example.com/download', 'a' * 64),
                            ('https://example.com/path%2Ftest.whl', 'a' * 64),
                            ('https://example.com/..%2Ftest.whl', 'a' * 64),
                            ('https://example.com/path%5Ctest.whl', 'a' * 64),
                            ('https://example.com/test.whl', '')]:
            result = subprocess.run(['bash', '-c',
                'source "$1"; WHEEL_URL="$2"; WHEEL_SHA256="$3"; WHEEL_FILE=""; prepare_wheel python3',
                'test', str(ROOT / 'mooncake-loadtest.sh'), url, digest], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_wheel_url_decodes_local_version_plus_without_changing_request(self):
        with tempfile.TemporaryDirectory() as root:
            url = 'https://example.com/package-1.0%2Bfix-cp313-cp313-linux_x86_64.whl'
            result = subprocess.run(['bash', '-c',
                'source "$1"; WORK="$2"; WHEEL_URL="$3"; WHEEL_SHA256="$(printf a%.0s {1..64})"; WHEEL_FILE=""; '
                'curl() { local last; for last in "$@"; do :; done; printf "%s" "$WHEEL_URL" > "$last"; }; '
                'prepare_wheel python3; printf "%s" "$WHEEL_FILE"',
                'test', str(ROOT / 'mooncake-loadtest.sh'), root, url], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = Path(result.stdout)
            self.assertEqual(artifact.name, 'package-1.0+fix-cp313-cp313-linux_x86_64.whl')
            self.assertEqual(artifact.read_text(), url)

    def test_read_files_are_initialized_to_exact_extent(self):
        result = subprocess.run(['bash', '-c', 'source "$1"; fs_fio_job objread /tmp/fio libaio 128 21233664 1', 'test', str(ROOT / 'mooncake-loadtest.sh')], capture_output=True, text=True, check=True)
        self.assertIn('filesize=128M\nsize=128M', result.stdout)
        self.assertIn('allow_file_create=0', result.stdout)


if __name__ == '__main__':
    unittest.main()
