"""Pure fixtures: these tests never inspect or mutate hardware."""
import copy
import unittest

from test_runner import module

accept = module("drive_acceptance")


def fixture():
    def node(name, kind, serial='', mounts=None, children=None):
        return dict(name=name, kname=name, path='/dev/' + name, type=kind,
                    serial=serial, size=1048576 + 4096, ro=False,
                    mountpoints=mounts or [], children=children or [])
    root = node('nvme0n1', 'disk', 'BOOT', children=[
        node('nvme0n1p1', 'part', mounts=['/boot']),
        node('nvme0n1p2', 'part', children=[node('dm-0', 'lvm', mounts=['/'])])])
    data = node('nvme1n1', 'disk', 'DATA')
    tree = {'blockdevices': [root, data]}
    nodes, edges = accept.flatten(tree)
    inv = dict(service_tag='EXPECTED', nodes=nodes, lsblk=tree,
               protected=accept.protection(nodes, edges, set(), {}, {}))
    return inv, edges


class Guards(unittest.TestCase):
    def setUp(self):
        self.inv, self.edges = fixture()
        self.entry = dict(device='/dev/nvme1n1', serial='DATA', bytes=1048576 + 4096)

    def test_proc_swaps_is_portable_and_decodes_escaped_paths(self):
        self.assertEqual(accept.swap_paths("Filename Type Size Used Priority\n"), set())
        self.assertEqual(accept.swap_paths("Filename Type Size Used Priority\n/dev/dm-1 partition 1024 0 -2\n/swap\\040file file 1024 0 -3\n"),
                         {'/dev/dm-1', '/swap file'})

    def test_only_explicit_matching_data_disk_is_accepted(self):
        self.assertEqual(len(accept.targets(self.inv, 'EXPECTED', [self.entry])), 1)

    def test_service_tag_mismatch_fails(self):
        with self.assertRaises(RuntimeError):
            accept.targets(self.inv, 'OTHER', [self.entry])

    def test_boot_and_root_backing_disk_is_never_writable(self):
        entry = dict(device='/dev/nvme0n1', serial='BOOT', bytes=1048576 + 4096)
        with self.assertRaises(RuntimeError):
            accept.targets(self.inv, 'EXPECTED', [entry])

    def test_serial_capacity_and_path_must_match(self):
        for change in [dict(serial='OTHER'), dict(bytes=1048576), dict(device='/dev/nvme1n1p1')]:
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                accept.targets(self.inv, 'EXPECTED', [{**self.entry, **change}])

    def test_empty_or_duplicate_allowlist_fails(self):
        for entries in [[], [self.entry, self.entry]]:
            with self.assertRaises(RuntimeError):
                accept.targets(self.inv, 'EXPECTED', entries)

    def test_mount_swap_holder_open_readonly_each_protects(self):
        for mode in ['mount', 'swap', 'holder', 'open', 'readonly']:
            inv = copy.deepcopy(self.inv)
            name = 'nvme1n1'
            if mode == 'mount':
                inv['nodes'][name]['mountpoints'] = ['/mnt/data']
            if mode == 'readonly':
                inv['nodes'][name]['ro'] = True
            inv['protected'] = accept.protection(inv['nodes'], self.edges,
                {'/dev/nvme1n1'} if mode == 'swap' else set(),
                {name: ['md127']} if mode == 'holder' else {},
                {name: ['123']} if mode == 'open' else {})
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                accept.targets(inv, 'EXPECTED', [self.entry])

    def test_both_legs_of_shared_mounted_md_are_protected(self):
        tree = {'blockdevices': [
            dict(kname=f'nvme{i}n1', name=f'nvme{i}n1', path=f'/dev/nvme{i}n1', type='disk',
                 children=[dict(kname='md127', path='/dev/md127', type='raid0',
                                mountpoints=['/mnt/l3'])]) for i in [0, 1]]}
        nodes, edges = accept.flatten(tree)
        protected = accept.protection(nodes, edges, set(), {}, {})
        self.assertTrue(protected['nvme0n1'])
        self.assertTrue(protected['nvme1n1'])

    def test_full_write_and_read_required_not_just_fio_exit_success(self):
        data = {'jobs': [dict(error=0, read=dict(io_bytes=4096), write=dict(io_bytes=4096))]}
        self.assertTrue(accept.fio_pass(data, 4096, True))
        self.assertFalse(accept.fio_pass(data, 8192, True))
        data['jobs'][0]['error'] = 84
        self.assertFalse(accept.fio_pass(data, 4096, True))

    def test_readonly_job_has_no_crc_write_or_discard(self):
        job = accept.fio_job('/dev/nvme0n1', 1048576 + 4096, False)
        self.assertIn('rw=read', job)
        self.assertNotIn('rw=write', job)
        self.assertNotIn('verify=crc32c', job)
        self.assertIn('offset=1048576', job)
        self.assertIn('size=4096', job)

    def test_crc_job_covers_tail_and_has_no_time_based_limit(self):
        job = accept.fio_job('/dev/nvme1n1', 1048576 + 4096, True)
        self.assertIn('verify=crc32c', job)
        self.assertIn('size=1048576', job)
        self.assertIn('size=4096', job)
        self.assertNotIn('runtime=', job)
        self.assertNotIn('time_based=', job)


if __name__ == '__main__':
    unittest.main()
