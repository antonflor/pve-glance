import importlib.util
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('glance', 'pve-glance.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def fs(mount='/', used=25, total=100, **extra):
    return dict({'mountpoint': mount, 'used-bytes': used, 'total-bytes': total,
                 'type': 'ext4', 'disk': [{'dev': '/dev/sda'}]}, **extra)


def cluster():
    return g._cluster('lab', [
        {'type': 'qemu', 'vmid': 100, 'node': 'pve', 'status': 'running'},
        {'type': 'qemu', 'vmid': 101, 'node': 'pve', 'status': 'stopped'},
        {'type': 'lxc', 'vmid': 102, 'node': 'pve', 'status': 'running', 'disk': 0, 'maxdisk': 100}])


class Disks(unittest.TestCase):
    def setUp(self):
        g._disk_cache.clear()

    def test_filesystems(self):
        rows = g.filesystems({'result': [fs('/data', 0), fs(), fs(), fs('/tmp', type='tmpfs'),
            fs('/bad', -1), fs('/zero', 0, 0), fs('/over', 200), fs('/str', '1'),
            fs('/network', disk=[]), None, fs('C:\\', 30, type='ntfs')]})
        self.assertEqual([x['mountpoint'] for x in rows], ['/', 'C:\\', '/data'])
        self.assertEqual(rows[-1]['disk'], 0)
        self.assertEqual(g.filesystems(None), [])
        self.assertEqual(g.filesystems({'result': {}}), [])

    def test_local_cache_and_stopped(self):
        with patch.object(g, '_pvesh_get', return_value={'result': [fs()]}) as read:
            data = g.guest_disks(cluster())
            self.assertEqual(data['guests'][0]['disk'], 25)
            self.assertEqual(data['guests'][0]['disk_status'], 'ok')
            self.assertEqual(data['guests'][2]['disk_status'], 'stopped')
            self.assertEqual(data['guests'][1]['disk'], 0)
            g.guest_disks(cluster())
            read.assert_called_once_with('/nodes/pve/qemu/100/agent/get-fsinfo', 5)
            with patch.object(g.time, 'monotonic', return_value=g.time.monotonic()+31):
                g.guest_disks(cluster())
            self.assertEqual(read.call_count, 2)

    def test_remote_and_target_isolation(self):
        t = dict(url='https://pve', token='secret', verify=True)
        with patch.object(g, '_http_get', return_value={'result': [fs('C:\\', 0)]}) as read:
            self.assertEqual(g.guest_disks(cluster(), t)['guests'][0]['disk'], 0)
            g.guest_disks(cluster(), dict(t, token='other'))
            self.assertEqual(read.call_count, 2)

    def test_failures_are_isolated_and_redacted(self):
        for error, status in [(RuntimeError('secret'), 'agent unavailable'),
                (subprocess.TimeoutExpired('pvesh', 5), 'agent timed out'),
                (HTTPError('secret', 403, 'denied', {}, None), 'permission denied')]:
            g._disk_cache.clear()
            with patch.object(g, '_pvesh_get', side_effect=error):
                data = g.guest_disks(cluster())
            self.assertTrue(data['ok'])
            self.assertEqual(data['guests'][0]['disk_status'], status)
            self.assertNotIn('secret', json.dumps(data))

    def test_empty_and_cache_eviction(self):
        with patch.object(g, '_pvesh_get', return_value={'result': []}):
            self.assertEqual(g.guest_disks(cluster())['guests'][0]['disk_status'], 'no filesystem data')
        g.guest_disks(g._cluster('lab', []))
        self.assertFalse(g._disk_cache)

    def test_collect_remote_outage_isolation(self):
        targets = [dict(name='good', url='https://good', token='a', verify=True),
                   dict(name='bad', url='https://bad', token='b', verify=True)]
        def read(target, path, timeout=g.API_TIMEOUT):
            if target['name'] == 'bad':
                raise RuntimeError('offline')
            if path == '/cluster/resources':
                return [{'type':'qemu', 'vmid':100, 'node':'pve', 'status':'running'}]
            return {'result':[fs()]}
        with patch.object(g, 'targets', return_value=targets), patch.object(g, '_http_get', side_effect=read):
            data = g.collect()
        self.assertEqual([c['ok'] for c in data['clusters']], [True, False])
        self.assertEqual(data['clusters'][0]['guests'][0]['disk_status'], 'ok')

    def test_pvesh_and_http_transport(self):
        with patch.object(g.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"result": []}'
            self.assertEqual(g._pvesh_get('/test', 5), {'result': []})
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
        with patch.object(g.urllib.request, 'urlopen') as read:
            read.return_value.__enter__.return_value.read.return_value = b'{"data":{"result":[]}}'
            t = dict(name='lab', url='https://pve', token='secret', verify=True)
            self.assertEqual(g._http_get(t, '/test', 5), {'result': []})
            self.assertEqual(read.call_args.args[0].full_url, 'https://pve/api2/json/test')
            self.assertEqual(read.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
    unittest.main()
