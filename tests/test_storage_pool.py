"""Storage ownership and recovery fixtures; never touch real block devices."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

SPEC = importlib.util.spec_from_file_location('storage_pool', Path(__file__).resolve().parents[1] / 'scripts/storage_pool.py')
pool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pool)


def fixture():
    entries = [dict(device=f'/dev/nvme{i}n1', serial=f'SERIAL{i}', bytes=1 << 40,
                    pv_uuid=f'PV{i}', pv_removed=False) for i in range(2)]
    groups = pool.groups_for(entries, {e['device']: (i, 512) for i, e in enumerate(entries)}, 'test', 128)
    pvs, vgs, segments = [], [], []
    for i, group in enumerate(groups):
        group.update(vg_uuid=f'VG{i}', lv_uuid=f'LV{i}', fs_uuid=None)
        pvs.append(dict(pv_name=entries[i]['device'], pv_uuid=f'PV{i}', vg_name=group['vg'], vg_uuid=f'VG{i}'))
        vgs.append(dict(vg_name=group['vg'], vg_uuid=f'VG{i}'))
        segments.append(dict(vg_name=group['vg'], lv_uuid=f'LV{i}', lv_path=group['device'],
            lv_size=str(128 << 30), segtype='linear', stripes='1', devices=entries[i]['device'] + '(0)'))
    return dict(entries=entries, groups=groups), pvs, vgs, segments


class StoragePoolTests(unittest.TestCase):
    def test_mixed_sectors_are_separate_and_singleton_is_linear(self):
        entries = [dict(device=f'/dev/nvme{i}n1') for i in range(23)]
        geometry = {e['device']: (int(i >= 11), 4096 if i == 0 else 512) for i, e in enumerate(entries)}
        groups = pool.groups_for(entries, geometry, 'abc', 128)
        self.assertEqual([(g['numa'], g['sector'], len(g['devices'])) for g in groups],
                         [(0, 512, 10), (0, 4096, 1), (1, 512, 12)])
        self.assertEqual(sum(len(g['devices']) for g in groups), 23)
        self.assertEqual(len({d for g in groups for d in g['devices']}), 23)

    def test_real_lvm_segment_report_is_parsed(self):
        document = {'report': [{'seg': [{'lv_uuid': 'nCD1DJ-EPhg-Sr23-fyCA-Ot9Z-pCcw-Dp0VNn',
            'segtype': 'striped', 'stripes': '11', 'lv_size': '137443147776'}]}]}
        self.assertEqual(pool.rows(json.dumps(document), 'seg'), document['report'][0]['seg'])
        self.assertEqual(pool.rows(json.dumps(document), 'lv'), [])

    def test_owned_metadata_passes_and_uuid_drift_fails(self):
        state, pvs, vgs, lvs = fixture()
        pool.validate_metadata(state, pvs, vgs, lvs)
        for kind, field in [('pv', 'pv_uuid'), ('vg', 'vg_uuid'), ('lv', 'lv_uuid')]:
            altered = copy.deepcopy((pvs, vgs, lvs))
            altered[{'pv': 0, 'vg': 1, 'lv': 2}[kind]][0][field] = 'WRONG'
            with self.subTest(kind=kind), self.assertRaises(pool.Refusal):
                pool.validate_metadata(state, *altered)

    def test_unrecorded_pv_after_interruption_is_never_adopted(self):
        state, pvs, vgs, lvs = fixture()
        state['entries'][0]['pv_uuid'] = None
        with self.assertRaisesRegex(pool.Refusal, 'unrecorded PV'):
            pool.validate_metadata(state, pvs, vgs, lvs)

    def test_duplicate_uuid_or_extra_vg_member_fails(self):
        state, pvs, vgs, lvs = fixture()
        for extra in [dict(pv_name='/dev/unknown', pv_uuid='PV0', vg_name='', vg_uuid=''),
                      dict(pv_name='/dev/unknown', pv_uuid='EXTRA', vg_name=state['groups'][0]['vg'], vg_uuid='VG0')]:
            with self.assertRaises(pool.Refusal):
                pool.validate_metadata(state, pvs + [extra], vgs, lvs)

    def test_unrecorded_lv_or_wrong_layout_fails(self):
        state, pvs, vgs, lvs = fixture()
        state['groups'][0]['lv_uuid'] = None
        with self.assertRaises(pool.Refusal):
            pool.validate_metadata(state, pvs, vgs, lvs)
        state['groups'][0]['lv_uuid'] = 'LV0'
        for change in [dict(devices='/dev/boot(0)'), dict(segtype='striped'), dict(lv_size='4096')]:
            with self.assertRaises(pool.Refusal):
                pool.validate_metadata(state, pvs, vgs, [{**lvs[0], **change}, lvs[1]])

    def test_recorded_cleanup_progress_passes_but_reappearing_pv_fails(self):
        state, pvs, vgs, lvs = fixture()
        state['entries'][0]['pv_removed'] = True
        state['groups'][0].update(lv_removed=True, vg_removed=True)
        pool.validate_metadata(state, pvs[1:], vgs[1:], lvs[1:])
        with self.assertRaises(pool.Refusal):
            pool.validate_metadata(state, pvs, vgs[1:], lvs[1:])

    def resume_fixture(self, root, inflight=None):
        module = Path(root, 'drive.py'); module.write_text('# empty fixture module\n')
        directory = Path(root, 'output'); directory.mkdir()
        entries = [dict(device='/dev/nvme1n1', serial='DATA', bytes=1024, pv_uuid='PV', pv_removed=False)]
        allowlist = Path(root, 'allowlist.json')
        allowlist.write_text(json.dumps([{k: entries[0][k] for k in ('device', 'serial', 'bytes')}]))
        state = dict(entries=entries, expected_tag='TAG', expected_drives=2,
                     module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),
                     groups=[dict(size_gib=128)], inflight=inflight)
        pool.save(directory / 'manifest.json', state)
        args = types.SimpleNamespace(out=directory, module=module, allowlist=allowlist,
            expected_tag='TAG', expected_drives=2, size_gib=128, resume=True, cleanup=False)
        return args

    def test_existing_output_requires_resume_and_matching_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.resume_fixture(root)
            self.assertEqual(pool.Pool(args).state['entries'][0]['pv_uuid'], 'PV')
            args.resume = False
            with self.assertRaisesRegex(pool.Refusal, 'resume'):
                pool.Pool(args)
            args.resume = True; args.expected_tag = 'OTHER'
            with self.assertRaises(pool.Refusal):
                pool.Pool(args)

    def test_unresolved_mutation_refuses_resume(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.resume_fixture(root, dict(argv=['pvcreate', '/dev/nvme1n1']))
            with self.assertRaisesRegex(pool.Refusal, 'unresolved ownership'):
                pool.Pool(args)

    def test_cleanup_checks_uuids_before_issuing_removal(self):
        state, pvs, vgs, lvs = fixture()
        pvs[0]['pv_uuid'] = 'DRIFT'
        instance = pool.Pool.__new__(pool.Pool)
        instance.state = state
        instance.guard = lambda: pool.validate_metadata(state, pvs, vgs, lvs)
        instance.run = Mock()
        with self.assertRaises(pool.Refusal):
            instance.cleanup()
        instance.run.assert_not_called()

    def test_cleanup_retains_nonempty_or_replaced_directories(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, 'owned'); directory.mkdir()
            (directory / 'not-ours').write_text('retain')
            st = directory.stat()
            instance = pool.Pool.__new__(pool.Pool)
            instance.manifest = Path(root, 'manifest.json')
            instance.state = dict(groups=[], entries=[], created_dirs={str(directory): [st.st_dev, st.st_ino]})
            instance.cleanup()
            self.assertEqual(instance.state['retained'], [str(directory)])
            self.assertTrue((directory / 'not-ours').exists())
            instance.state['created_dirs'][str(directory)] = [st.st_dev, st.st_ino + 1]
            with self.assertRaises(pool.Refusal):
                instance.cleanup()

    def test_mutation_writes_intent_before_command_and_preserves_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            instance = pool.Pool.__new__(pool.Pool)
            instance.manifest = Path(root, 'manifest.json')
            instance.state = {'inflight': None}
            instance.guard = Mock()
            def fail(argv):
                on_disk = json.loads(instance.manifest.read_text())
                self.assertEqual(on_disk['inflight']['argv'], argv)
                raise RuntimeError('interrupted')
            instance.run = fail
            record = Mock()
            with self.assertRaises(RuntimeError):
                instance.mutate(['pvcreate', '/dev/test'], record)
            record.assert_not_called()
            self.assertIsNotNone(json.loads(instance.manifest.read_text())['inflight'])

    def test_completed_resume_does_not_initialize_pvs_again(self):
        state, _, _, _ = fixture()
        for group in state['groups']:
            group.update(fs_uuid='FS', mounted=True)
        instance = pool.Pool.__new__(pool.Pool)
        instance.state = state
        instance.mutate = Mock()
        instance.persist = Mock()
        instance.provision()
        instance.mutate.assert_not_called()
        self.assertEqual(instance.state['status'], 'mounted')

    def test_mount_directory_must_be_owned_empty_and_unreplaced(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, 'target'); directory.mkdir()
            instance = pool.Pool.__new__(pool.Pool)
            instance.state = {'created_dirs': {}}
            with self.assertRaises(pool.Refusal):
                instance.make_directory(directory)
            st = directory.stat()
            instance.state['created_dirs'][str(directory)] = [st.st_dev, st.st_ino]
            (directory / 'data').write_text('must not hide this')
            with self.assertRaises(pool.Refusal):
                instance.make_directory(directory)
            self.assertTrue((directory / 'data').exists())

    def test_striped_layout_checks_chunk_and_exact_extent_rounded_size(self):
        group = dict(name='stripe', device='/dev/test/data', devices=['/dev/a', '/dev/b', '/dev/c'],
                     size_gib=128, lv_uuid='LV')
        stripe_extent = 3 * (4 << 20)
        size = (((128 << 30) + stripe_extent - 1) // stripe_extent) * stripe_extent
        row = dict(lv_path=group['device'], lv_uuid='LV', lv_size=str(size), segtype='striped',
                   stripes='3', stripe_size=str(256 << 10), devices='/dev/a(0),/dev/b(0),/dev/c(0)')
        pool.validate_layout([row], group)
        for change in [dict(stripe_size=str(64 << 10)), dict(lv_size=str(size + stripe_extent))]:
            with self.assertRaises(pool.Refusal):
                pool.validate_layout([{**row, **change}], group)


if __name__ == '__main__':
    unittest.main()
