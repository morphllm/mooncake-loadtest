"""State and acceptance fixtures; no SSH or block-device operations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('qualify_node', Path(__file__).resolve().parents[1] / 'scripts/qualify_node.py')
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


def node():
    return dict(name='storage1', ssh='storage1', expected_tag='TAG', expected_drives=2,
                boot_serial='BOOT', allowlist=[dict(device='/dev/nvme0n1', serial='DATA', bytes=4096)],
                wheel=dict(flavor='non-cuda', version='1.0', sha256='a'*64, file='/private/wheel.whl'))


class QualificationTests(unittest.TestCase):
    def test_explicit_boot_exclusion(self):
        c = node(); c['boot_serial'] = 'DATA'
        with self.assertRaisesRegex(ValueError, 'boot serial'):
            q.normalize_node(c)

    def test_no_fractional_workers(self):
        c = node(); c['workers'] = 1.5
        with self.assertRaisesRegex(ValueError, 'integer'):
            q.normalize_node(c)

    def test_full_cohort_required(self):
        c = node(); c['expected_drives'] = 3
        with self.assertRaisesRegex(ValueError, 'every data drive'):
            q.normalize_node(c)

    def test_failed_stage_never_repeats_erase(self):
        with tempfile.TemporaryDirectory() as root:
            w = q.Workflow(node(), Path(root)/'artifacts')
            w.state['steps']['erase'] = dict(status='running')
            w.command = lambda *_: self.fail('must not repeat erase')
            with self.assertRaisesRegex(RuntimeError, 'preserved'):
                w.stage('erase', ['erase'], [Path(root)/'missing'], lambda: False)

    def test_completed_evidence_cannot_change(self):
        with tempfile.TemporaryDirectory() as root:
            w = q.Workflow(node(), Path(root)/'artifacts')
            result = w.out/'result.json'; result.write_text('{}')
            w.command = lambda *_: 0
            w.stage('read', ['read'], [result])
            result.write_text('{"changed":true}')
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                w.stage('read', ['read'], [result])

    def test_crash_after_valid_evidence_can_resume_without_command(self):
        with tempfile.TemporaryDirectory() as root:
            w = q.Workflow(node(), Path(root)/'artifacts')
            result = w.out/'result.json'; result.write_text('{}')
            w.state['steps']['read'] = dict(status='running')
            w.command = lambda *_: self.fail('must reuse verified evidence')
            w.stage('read', ['read'], [result], lambda: True)
            self.assertTrue(w.state['steps']['read']['recovered_from_evidence'])

    def test_config_change_refused(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)/'artifacts'; q.Workflow(node(), out)
            c = node(); c['boot_serial'] = 'OTHER'
            with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
                q.Workflow(c, out, resume=True)

    def test_no_cleanup_before_complete(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, 'complete qualification'):
                q.Workflow(node(), Path(root)/'artifacts').cleanup()

    def test_crc_requires_identity_health_and_exact_fio_proof(self):
        with tempfile.TemporaryDirectory() as root:
            entry = node()['allowlist'][0]; d = Path(root)/'DATA'; d.mkdir()
            result = dict(entry, status='FULL_BYTES_VERIFIED', health=dict(status='NO_NEW_RECORDED_FAULT'))
            q.save(d/'result.json', result); q.save(d/'fio.json', {'bytes':4096})
            module = types.SimpleNamespace(fio_pass=lambda data, size, writable: data['bytes']==size and writable)
            self.assertTrue(q.verified_crc(root, [entry], module))
            q.save(d/'fio.json', {'bytes':2048})
            self.assertFalse(q.verified_crc(root, [entry], module))

    def test_duplicate_controller_rows_cannot_accept(self):
        with tempfile.TemporaryDirectory() as root:
            c = q.normalize_node(node()); c['require_rdma'] = False
            w = q.Workflow(c, Path(root)/'artifacts')
            for sub in ('data-reads','boot-reads','selftests','mooncake/server'):
                (w.out/sub).mkdir(parents=True)
            read = dict(serial='DATA', status='MEASURED', health_clean=True, speeds_GB_s=[5.6]*3)
            q.save(w.out/'data-reads/results.json', {'drives':[read]})
            q.save(w.out/'boot-reads/results.json', {'drives':[dict(read,serial='BOOT')]})
            q.save(w.out/'selftests/results.json', {'drives':[dict(serial='DATA',status='PASS',health_clean=True)]*2})
            q.save(w.out/'mooncake/server/info.json', {'lanes':[{'idx':0}]})
            q.save(w.out/'mooncake/report.json', {'rows':[{'check':name,'verdict':'PASS'} for name in
                ('lane 0 master: SSD hits','lane 0 master: SSD bytes','lane 0 L3 read attribution')]})
            with patch.object(q, 'verified_crc', return_value=True): w.finish()
            result = json.loads((w.out/'acceptance.json').read_text())
            self.assertFalse(result['controller_short_tests'])
            self.assertFalse(result['acceptance_ready'])

    def test_profile_sampling_failure_cannot_accept_clean_report(self):
        with tempfile.TemporaryDirectory() as root:
            d = Path(root)
            q.save(d/'profile.json', dict(returncode=1, command_returncode=0, restore_errors=[], sampling_errors=['lost child maps']))
            q.save(d/'report.json', {'rows':[{'check':'throughput','verdict':'PASS'}]})
            self.assertFalse(q.profile_complete(d/'profile.json', d/'report.json'))

    def test_partial_controller_cohort_cannot_resume_as_complete(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'tests.json'
            q.save(p, {'drives':[{'serial':'DATA','status':'INCOMPLETE_RUNNING'}]})
            self.assertFalse(q.cohort_complete(p, ['DATA']))
            q.save(p, {'drives':[{'serial':'DATA','status':'FAILED'}]})
            self.assertTrue(q.cohort_complete(p, ['DATA']))

    def test_mooncake_requires_l3_evidence_and_does_not_claim_rdma(self):
        with tempfile.TemporaryDirectory() as root:
            d=Path(root); (d/'server').mkdir()
            q.save(d/'server/info.json', {'lanes':[{'idx':0}]})
            q.save(d/'report.json', {'rows':[]})
            self.assertFalse(q.mooncake_verdict(d)['passed'])
            rows=[{'check':x,'verdict':'PASS'} for x in ('lane 0 master: SSD hits','lane 0 master: SSD bytes','lane 0 L3 read attribution')]
            q.save(d/'report.json', {'rows':rows})
            verdict=q.mooncake_verdict(d)
            self.assertTrue(verdict['passed'])
            self.assertEqual(verdict['remote_rdma'],'NOT_QUALIFIED_SINGLE_HOST')

if __name__ == '__main__':
    unittest.main()
