import errno
import importlib.util
import json
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def sample(**changes):
    result = {
        'gbps': 2.5, 'offered_gbps': 3.0, 'bytes': 25_000_000_000,
        'objs': 1000, 'seconds': 10, 'workers': 4, 'window': [100, 110],
        'lat_ms': {'p50': 1.5, 'p99': 4.0, 'p999': 8.0},
        'misses': 0, 'errors': {}, 'mismatch': 0,
        'deadline_refused': 0, 'refusals': 0, **changes,
    }
    if 'bytes' not in changes:
        result['bytes'] = int(result['gbps'] * result['seconds'] * 1e9)
    if 'objs' not in changes:
        result['objs'] = result['bytes'] // 25_000_000
    return result


def benchmark():
    return {
        'config': {'lanes': [{'idx': 0}], 'object_bytes': 25_000_000},
        'phases': {
            'put_sustained@1': {'0': sample(gbps=1.0, workers=1)},
            'put_sustained@4': {'0': sample(deadline_refused=2, refusals=12)},
            'get_dram@1': {'0': sample(gbps=1.75, workers=1)},
            'get_dram@4': {'0': sample(gbps=4.0)},
            'get_nvme': {'0': sample(misses=3)},
            'mixed': {'0': {'put': sample(gbps=1.25), 'get': sample(gbps=2.0)}},
        },
    }


def smart_snapshot(**changes):
    return {
        'critical_warning': 0, 'media_errors': 0, 'num_err_log_entries': 7,
        'percent_used': 0, 'avail_spare': 100, 'spare_thresh': 10,
        'temperature': 310, 'power_on_hours': 24, 'unsafe_shutdowns': 0,
        'warning_temp_time': 0, 'critical_comp_time': 0, **changes,
    }


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = tempfile.TemporaryDirectory()
        subprocess.run(
            ['bash', '-c', 'source "$1"; mkdir -p "$LIB"; write_payload',
             'test', str(ROOT / 'mooncake-loadtest.sh')],
            env={**os.environ, 'MCL_WORK': cls.payload.name}, check=True,
        )
        cls.script = Path(cls.payload.name, 'lib/report.py')
        spec = importlib.util.spec_from_file_location('report_under_test', cls.script)
        cls.report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.payload.cleanup()

    def test_traffic_preserves_numeric_measurements_and_latency(self):
        rows = self.report.traffic_rows(benchmark())
        row = next(r for r in rows if r['workload'] == 'get_dram@4')
        self.assertEqual(row['gbps'], 4.0)
        self.assertEqual(row['gbits'], 32.0)
        self.assertEqual(row['objps'], 160.0)
        self.assertEqual(row['lat_ms']['p99'], 4.0)
        self.assertEqual(row['status'], 'INFO')
        decoded = json.loads(json.dumps(rows))
        self.assertIsInstance(decoded[0]['gbps'], (int, float))
        self.assertIsInstance(decoded[0]['seconds'], (int, float))

    def test_capacity_refusals_are_amber_without_becoming_errors(self):
        rows = self.report.traffic_rows(benchmark())
        row = next(r for r in rows if r['workload'] == 'put_sustained@4')
        self.assertEqual(row['status'], 'WARN')
        self.assertEqual(row['detail'], 'capacity limited')
        self.assertEqual(row['deadline_refused'], 2)
        self.assertEqual(row['errors'], 0)
        self.assertEqual(row['bytes'], 25_000_000_000)

    def test_misses_corruption_and_zero_payload_are_invalid(self):
        for changes in ({'misses': 1}, {'mismatch': 1}, {'errors': {'-800': 2}},
                        {'bytes': 0, 'gbps': 0, 'objs': 0}):
            with self.subTest(changes=changes):
                bench = {'config': {'lanes': [{'idx': 0}]},
                         'phases': {'get_nvme': {'0': sample(**changes)}}}
                row, = self.report.traffic_rows(bench)
                self.assertEqual(row['status'], 'FAIL')
                self.assertEqual(row['detail'], 'invalid/errorful')

    def test_mixed_rows_keep_direction_rates_and_aggregate(self):
        rows = self.report.traffic_rows(benchmark())
        mixed = {r['workload']: r for r in rows if r['workload'].startswith('mixed')}
        self.assertEqual(mixed['mixed put']['gbps'], 1.25)
        self.assertEqual(mixed['mixed get']['gbps'], 2.0)
        self.assertEqual(mixed['mixed aggregate']['gbps'], 3.25)
        self.assertEqual(mixed['mixed aggregate']['seconds'], 10)

    def test_mismatched_mixed_windows_do_not_fabricate_an_aggregate(self):
        bench = {'config': {'lanes': [{'idx': 0}]}, 'phases': {'mixed': {'0': {
            'put': sample(window=[100, 110]), 'get': sample(window=[101, 111]),
        }}}}
        rows = self.report.traffic_rows(bench)
        aggregate = next(r for r in rows if r['workload'] == 'mixed aggregate')
        self.assertEqual(aggregate['status'], 'FAIL')
        self.assertIsNone(aggregate['gbps'])

    def test_missing_or_zero_measurement_time_cannot_be_clean(self):
        for seconds in (None, 0):
            with self.subTest(seconds=seconds):
                bench = {'config': {'lanes': [{'idx': 0}]}, 'phases': {
                    'get_dram@1': {'0': sample(seconds=seconds, bytes=25_000_000_000)},
                }}
                row, = self.report.traffic_rows(bench)
                self.assertEqual(row['status'], 'FAIL')

    def test_missing_evidence_creates_no_measurements(self):
        self.assertEqual(self.report.traffic_rows(None), [])
        self.assertEqual(self.report.traffic_rows({'phases': {}}), [])
        with tempfile.TemporaryDirectory() as directory:
            hardware = self.report.hardware_rows({}, Path(directory))
        self.assertEqual(hardware['drives'], [])
        self.assertEqual(hardware['nics'], [])
        page = self.report.report_html({'traffic': [], 'hardware': hardware})
        self.assertIn('<html', page.lower())
        self.assertNotIn('NaN', page)

    def test_hardware_uses_fio_measurements_and_keeps_missing_rates_unknown(self):
        inventory = {
            'nvme': {'nvme0n1': {'model': 'Synthetic drive', 'serial': 'TEST-ONLY',
                                'fw': 'test-fw', 'bytes': 100_000_000_000,
                                'pcie': {'cur': '16.0 x4', 'max': '16.0 x4', 'numa': 0},
                                'smart': {'temperature': 303, 'percent_used': 0}}},
            'nics': {'mlx5_0': {'hca': 'Synthetic NIC', 'fw': 'test-fw',
                                'pcie': {'cur': '16.0 x16', 'numa': 0},
                                'ports': {'1': {'rate': '200 Gb/sec', 'state': 'ACTIVE'}},
                                'ipv4': {'eth0': '192.0.2.10'},
                                'speed_mbit': {'eth0': 200000}}},
        }
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory, 'nvme')
            data.mkdir()
            (data / 'raw-seqread.json').write_text(json.dumps({'jobs': [{
                'jobname': 'nvme0n1', 'error': 0,
                'read': {'bw_bytes': 2_500_000_000, 'iops': 2500,
                         'clat_ns': {'percentile': {'99.000000': 2_000_000}}},
            }]}))
            hardware = self.report.hardware_rows(inventory, Path(directory))
        drive, = hardware['drives']
        self.assertEqual(drive['seqread_gbps'], 2.5)
        self.assertEqual(drive['seqread_p99_ms'], 2.0)
        self.assertIsNone(drive['objread_gbps'])
        nic, = hardware['nics']
        self.assertEqual(nic['rate'], '200 Gb/sec')

    def test_inactive_or_unmeasured_nic_rate_remains_unknown(self):
        for state, speed in (('DOWN', 200000), ('ACTIVE', None), ('ACTIVE', 0)):
            with self.subTest(state=state, speed=speed):
                inventory = {'nics': {'mlx5_0': {
                    'ports': {'1': {'rate': '400 Gb/sec', 'state': state}},
                    'speed_mbit': {} if speed is None else {'eth0': speed},
                }}}
                with tempfile.TemporaryDirectory() as directory:
                    hardware = self.report.hardware_rows(inventory, Path(directory))
                nic, = hardware['nics']
                self.assertIsNone(nic['rate'])
                self.assertIsNone(nic['rate_gbit'])
                self.assertEqual(nic['raw_port_rate'], '400 Gb/sec')

    def test_healthy_smart_is_scoped_to_reported_faults(self):
        health = self.report.smart_health(smart_snapshot(), smart_snapshot())
        self.assertEqual(health['health_status'], 'PASS')
        self.assertIn('no smart fault', health['health_label'].lower())
        self.assertNotIn('qualified', health['health_label'].lower())
        self.assertEqual(health['critical_warning'], 0)
        self.assertEqual(health['media_errors_delta'], 0)
        self.assertEqual(health['wear_percent'], 0)
        self.assertEqual(health['spare_percent'], 100)
        self.assertAlmostEqual(health['temperature_c'], 36.85, delta=.2)

    def test_smart_critical_warning_or_new_media_error_is_failed(self):
        for changes in ({'critical_warning': 1}, {'media_errors': 1}):
            with self.subTest(changes=changes):
                health = self.report.smart_health(smart_snapshot(), smart_snapshot(**changes))
                self.assertEqual(health['health_status'], 'FAIL')
        health = self.report.smart_health(smart_snapshot(), smart_snapshot(critical_warning=1))
        self.assertTrue(health['critical_warning_decoded'])
        self.assertEqual(int(health['critical_warning_hex'], 16), 1)
        incomplete = self.report.smart_health({}, smart_snapshot(critical_warning=1))
        self.assertEqual(incomplete['health_status'], 'FAIL')

    def test_missing_smart_remains_unknown(self):
        health = self.report.smart_health({}, {})
        self.assertEqual(health['health_status'], 'SKIP')
        self.assertIsNone(health['critical_warning'])
        self.assertIsNone(health['temperature_c'])
        self.assertIsNone(health['wear_percent'])

    def test_error_log_history_is_not_a_new_failure(self):
        health = self.report.smart_health(smart_snapshot(), smart_snapshot())
        self.assertNotEqual(health['health_status'], 'FAIL')
        self.assertEqual(health['error_log_entries'], 7)
        self.assertEqual(health['error_log_delta'], 0)

    def test_new_error_log_obeys_explicit_strict_and_lenient_modes(self):
        for strict, expected in ((True, 'FAIL'), (False, 'WARN')):
            with self.subTest(strict=strict), patch.object(self.report, 'STRICT', strict):
                health = self.report.smart_health(smart_snapshot(), smart_snapshot(num_err_log_entries=8))
                self.assertEqual(health['health_status'], expected)
                self.assertEqual(health['error_log_delta'], 1)

    def test_wear_and_reset_counters_are_warnings(self):
        for changes in ({'percent_used': 80}, {'num_err_log_entries': 6}):
            with self.subTest(changes=changes):
                health = self.report.smart_health(smart_snapshot(), smart_snapshot(**changes))
                self.assertEqual(health['health_status'], 'WARN')

    def test_namespace_health_joins_its_controller_not_a_prefix_neighbor(self):
        inventory = {'nvme': {
            'nvme12n1': {'model': 'Synthetic', 'serial': 'TEST-ONLY'},
            'nvme1n1': {'model': 'Synthetic', 'ctrl': 'nvme1'},
        }}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'before.json').write_text(json.dumps({'nvme': {
                'nvme12': smart_snapshot(), 'nvme1': smart_snapshot(),
            }}))
            (root / 'after.json').write_text(json.dumps({'nvme': {
                'nvme12': smart_snapshot(temperature=313),
                'nvme1': smart_snapshot(critical_warning=2),
            }}))
            hardware = self.report.hardware_rows(inventory, root)
        drives = {drive['device']: drive for drive in hardware['drives']}
        self.assertEqual(drives['nvme12n1']['controller'], 'nvme12')
        self.assertEqual(drives['nvme12n1']['health_status'], 'PASS')
        self.assertAlmostEqual(drives['nvme12n1']['temperature_c'], 39.85, delta=.2)
        self.assertEqual(drives['nvme12n1']['serial'], 'TEST-ONLY')
        self.assertEqual(drives['nvme1n1']['health_status'], 'FAIL')
        page = self.report.report_html({'hardware': hardware, 'traffic': []})
        self.assertIn('TEST-ONLY', page)
        self.assertIn(drives['nvme12n1']['health_label'], page)
        self.assertIn(drives['nvme1n1']['health_label'], page)

    def test_last_self_test_pass_supports_both_snapshot_schemas(self):
        logs = (
            {'Current Device Self-Test Operation': 0,
             'List of Valid Reports': [{'Self test result': 0}]},
            {'current_operation': 0, 'valid_reports': [{'result': 0}]},
        )
        for log in logs:
            with self.subTest(log=log):
                test = self.report.self_test_health(log)
                self.assertEqual(test['self_test_status'], 'PASS')
                self.assertEqual(test['self_test_result'], 0)
                self.assertEqual(test['self_test_current_operation'], 0)
                self.assertIn('last recorded', test['self_test_label'])
                self.assertNotIn('qualified', test['self_test_label'])

    def test_self_test_unknown_running_failure_and_abort_classifications(self):
        cases = [({}, 'SKIP'),
                 ({'current_operation': 0, 'valid_reports': [{'result': 15}]}, 'SKIP'),
                 ({'valid_reports': [{'result': 0}]}, 'WARN'),
                 ({'current_operation': 1, 'valid_reports': [{'result': 0}]}, 'WARN')]
        cases += [({'current_operation': 0, 'valid_reports': [{'result': code}]}, 'FAIL')
                  for code in (5, 6, 7)]
        cases += [({'current_operation': 0, 'valid_reports': [{'result': code}]}, 'WARN')
                  for code in (1, 2, 3, 4, 8, 9)]
        for log, expected in cases:
            with self.subTest(log=log):
                test = self.report.self_test_health(log)
                self.assertEqual(test['self_test_status'], expected)

    def test_failed_self_test_propagates_through_controller_health_join(self):
        inventory = {'nvme': {'nvme12n1': {}, 'nvme1n1': {}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = {'nvme12': smart_snapshot(), 'nvme1': smart_snapshot()}
            (root / 'before.json').write_text(json.dumps({'nvme': snapshots}))
            (root / 'after.json').write_text(json.dumps({
                'nvme': snapshots,
                'nvme_selftest': {
                    'nvme12': {'Current Device Self-Test Operation': 0,
                               'List of Valid Reports': [{'Self test result': 0}]},
                    'nvme1': {'current_operation': 0, 'valid_reports': [{'result': 7}]},
                },
            }))
            hardware = self.report.hardware_rows(inventory, root)
        drives = {drive['device']: drive for drive in hardware['drives']}
        self.assertEqual(drives['nvme12n1']['self_test_status'], 'PASS')
        self.assertEqual(drives['nvme12n1']['health_status'], 'PASS')
        self.assertEqual(drives['nvme1n1']['self_test_status'], 'FAIL')
        self.assertEqual(drives['nvme1n1']['self_test_result'], 7)
        self.assertEqual(drives['nvme1n1']['health_status'], 'FAIL')

    def test_html_escapes_untrusted_host_checks_and_notes(self):
        attack = '<img src=x onerror="alert(1)">'
        document = {
            'meta': {'host': attack}, 'qualification': attack,
            'traffic': self.report.traffic_rows(benchmark()),
            'hardware': {'drives': [], 'nics': []},
            'rows': [{'section': attack, 'check': attack, 'measured': attack,
                      'bar': attack, 'verdict': 'FAIL', 'detail': attack}],
            'bottlenecks': [], 'notes': [attack],
        }
        page = self.report.report_html(document)
        self.assertNotIn(attack, page)
        self.assertIn('&lt;img', page)
        self.assertIn('Hot candidate GET', page)
        self.assertIn('Mixed aggregate', page)

    def test_minimal_run_writes_html_json_and_plain_terminal_with_no_color(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / 'server').mkdir()
            (run / 'client').mkdir()
            (run / 'meta.json').write_text(json.dumps({'mode': 'single', 'host': 'synthetic'}))
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(
                    [sys.executable, str(self.script), str(run)], stdout=slave,
                    stderr=subprocess.PIPE, env={**os.environ, 'NO_COLOR': '1'},
                )
                os.close(slave)
                slave = None
                terminal = bytearray()
                while True:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    terminal.extend(chunk)
                _, stderr = process.communicate(timeout=10)
                self.assertIn(process.returncode, (0, 1), stderr.decode())
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)
            self.assertNotIn(b'\x1b[', terminal)
            document = json.loads((run / 'report.json').read_text())
            self.assertEqual(document['traffic'], [])
            self.assertIn('hardware', document)
            self.assertIn('qualification', document)
            self.assertIn('<html', (run / 'report.html').read_text().lower())
            self.assertNotIn('\x1b[', (run / 'report.txt').read_text())

    def test_report_mode_refreshes_an_installed_older_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            interpreter = work / 'venv/bin/python'
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(sys.executable)
            (work / 'lib').mkdir()
            (work / 'lib/report.py').write_text('import sys\nsys.exit(77)\n')
            run = work / 'run'
            (run / 'server').mkdir(parents=True)
            (run / 'client').mkdir()
            (run / 'meta.json').write_text(json.dumps({'mode': 'single', 'host': 'synthetic'}))
            completed = subprocess.run(
                ['bash', '-c', 'source "$1"; uname() { echo Linux; }; main report "$2"',
                 'test', str(ROOT / 'mooncake-loadtest.sh'), str(run)],
                env={**os.environ, 'MCL_WORK': directory, 'NO_COLOR': '1'},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertTrue((run / 'report.html').is_file())
            document = json.loads((run / 'report.json').read_text())
            self.assertEqual(document['traffic'], [])


if __name__ == '__main__':
    unittest.main()
