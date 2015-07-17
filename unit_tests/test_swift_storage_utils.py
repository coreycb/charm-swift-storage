import shutil
import tempfile

from mock import call, patch, MagicMock
from test_utils import CharmTestCase, patch_open

import lib.swift_storage_utils as swift_utils


TO_PATCH = [
    'apt_update',
    'apt_upgrade',
    'log',
    'config',
    'configure_installation_source',
    'mkdir',
    'mount',
    'check_call',
    'call',
    'ensure_block_device',
    'clean_storage',
    'is_block_device',
    'is_device_mounted',
    'get_os_codename_package',
    'get_os_codename_install_source',
    'unit_private_ip',
    'service_restart',
    '_save_script_rc',
    'lsb_release',
]


PROC_PARTITIONS = """
major minor  #blocks  name

   8        0  732574584 sda
   8        1     102400 sda1
   8        2  307097600 sda2
   8        3          1 sda3
   8        5  146483200 sda5
   8        6    4881408 sda6
   8        7  274004992 sda7
   8       16  175825944 sdb
   9        0  732574584 vda
  10        0  732574584 vdb
  10        0  732574584 vdb1
 104        0 1003393784 cciss/c0d0
 105        0 1003393784 cciss/c1d0
 105        1   86123689 cciss/c1d0p1
 252        0   20971520 dm-0
 252        1   15728640 dm-1
"""

SCRIPT_RC_ENV = {
    'OPENSTACK_PORT_ACCOUNT': 6002,
    'OPENSTACK_PORT_CONTAINER': 6001,
    'OPENSTACK_PORT_OBJECT': 6000,
    'OPENSTACK_SWIFT_SERVICE_ACCOUNT': 'account-server',
    'OPENSTACK_SWIFT_SERVICE_CONTAINER': 'container-server',
    'OPENSTACK_SWIFT_SERVICE_OBJECT': 'object-server',
    'OPENSTACK_URL_ACCOUNT':
    'http://10.0.0.1:6002/recon/diskusage|"mounted":true',
    'OPENSTACK_URL_CONTAINER':
    'http://10.0.0.1:6001/recon/diskusage|"mounted":true',
    'OPENSTACK_URL_OBJECT':
    'http://10.0.0.1:6000/recon/diskusage|"mounted":true'
}


REAL_WORLD_PARTITIONS = """
major minor  #blocks  name

   8        0  117220824 sda
   8        1  117219800 sdb
   8       16  119454720 sdb1
"""


class SwiftStorageUtilsTests(CharmTestCase):

    def setUp(self):
        super(SwiftStorageUtilsTests, self).setUp(swift_utils, TO_PATCH)
        self.config.side_effect = self.test_config.get

    def test_ensure_swift_directories(self):
        with patch('os.path.isdir') as isdir:
            isdir.return_value = False
            swift_utils.ensure_swift_directories()
            ex_dirs = [
                call('/etc/swift', owner='swift', group='swift'),
                call('/var/cache/swift', owner='swift', group='swift'),
                call('/srv/node', owner='swift', group='swift')
            ]
        self.assertEquals(ex_dirs, self.mkdir.call_args_list)

    def test_swift_init_nonfatal(self):
        swift_utils.swift_init('all', 'start')
        self.call.assert_called_with(['swift-init', 'all', 'start'])

    def test_swift_init_fatal(self):
        swift_utils.swift_init('all', 'start', fatal=True)
        self.check_call.assert_called_with(['swift-init', 'all', 'start'])

    def test_fetch_swift_rings(self):
        url = 'http://someproxynode/rings'
        swift_utils.SWIFT_CONF_DIR = tempfile.mkdtemp()
        try:
            swift_utils.fetch_swift_rings(url)
            wgets = []
            for s in ['account', 'object', 'container']:
                _c = call(['wget', '%s/%s.ring.gz' % (url, s),
                           '--retry-connrefused', '-t', '10',
                           '-O', '/etc/swift/%s.ring.gz' % s])
                wgets.append(_c)
            self.assertEquals(wgets, self.check_call.call_args_list)
        except:
            shutil.rmtree(swift_utils.SWIFT_CONF_DIR)

    def test_determine_block_device_no_config(self):
        self.test_config.set('block-device', None)
        self.assertEquals(swift_utils.determine_block_devices(), None)

    def _fake_ensure(self, bdev):
        # /dev/vdz is a missing dev
        if '/dev/vdz' in bdev:
            return None
        else:
            return bdev.split('|').pop(0)

    @patch.object(swift_utils, 'ensure_block_device')
    def test_determine_block_device_single_dev(self, _ensure):
        _ensure.side_effect = self._fake_ensure
        bdevs = '/dev/vdb'
        self.test_config.set('block-device', bdevs)
        result = swift_utils.determine_block_devices()
        self.assertEquals(['/dev/vdb'], result)

    @patch.object(swift_utils, 'ensure_block_device')
    def test_determine_block_device_multi_dev(self, _ensure):
        _ensure.side_effect = self._fake_ensure
        bdevs = '/dev/vdb /dev/vdc /tmp/swift.img|1G'
        self.test_config.set('block-device', bdevs)
        result = swift_utils.determine_block_devices()
        ex = ['/dev/vdb', '/dev/vdc', '/tmp/swift.img']
        self.assertEquals(ex, result)

    @patch.object(swift_utils, 'ensure_block_device')
    def test_determine_block_device_with_missing(self, _ensure):
        _ensure.side_effect = self._fake_ensure
        bdevs = '/dev/vdb /srv/swift.img|20G /dev/vdz'
        self.test_config.set('block-device', bdevs)
        result = swift_utils.determine_block_devices()
        ex = ['/dev/vdb', '/srv/swift.img']
        self.assertEqual(ex, result)

    @patch.object(swift_utils, 'find_block_devices')
    @patch.object(swift_utils, 'ensure_block_device')
    def test_determine_block_device_guess_dev(self, _ensure, _find):
        _ensure.side_effect = self._fake_ensure
        self.test_config.set('block-device', 'guess')
        _find.return_value = ['/dev/vdb', '/dev/sdb']
        result = swift_utils.determine_block_devices()
        self.assertTrue(_find.called)
        self.assertEquals(result, ['/dev/vdb', '/dev/sdb'])

    def test_mkfs_xfs(self):
        swift_utils.mkfs_xfs('/dev/sdb')
        self.check_call.assert_called_with(
            ['mkfs.xfs', '-f', '-i', 'size=1024', '/dev/sdb']
        )

    @patch.object(swift_utils, 'clean_storage')
    @patch.object(swift_utils, 'mkfs_xfs')
    @patch.object(swift_utils, 'determine_block_devices')
    def test_setup_storage_no_overwrite(self, determine, mkfs, clean):
        determine.return_value = ['/dev/vdb']
        self.test_config.set('overwrite', 'false')
        swift_utils.setup_storage()
        self.assertFalse(clean.called)
        calls = [call(['chown', '-R', 'swift:swift', '/srv/node/']),
                 call(['chmod', '-R', '0755', '/srv/node/'])]
        self.check_call.assert_has_calls(calls)

    @patch.object(swift_utils, 'clean_storage')
    @patch.object(swift_utils, 'mkfs_xfs')
    @patch.object(swift_utils, 'determine_block_devices')
    def test_setup_storage_overwrite(self, determine, mkfs, clean):
        determine.return_value = ['/dev/vdb']
        self.test_config.set('overwrite', 'True')
        swift_utils.setup_storage()
        clean.assert_called_with('/dev/vdb')
        self.mkdir.assert_called_with('/srv/node/vdb', owner='swift',
                                      group='swift')
        self.mount.assert_called('/dev/vdb', '/srv/node/vdb', persist=True)
        calls = [call(['chown', '-R', 'swift:swift', '/srv/node/']),
                 call(['chmod', '-R', '0755', '/srv/node/'])]
        self.check_call.assert_has_calls(calls)

    def _fake_is_device_mounted(self, device):
        if device in ["/dev/sda", "/dev/vda", "/dev/cciss/c0d0"]:
            return True
        else:
            return False

    def test_find_block_devices(self):
        self.is_block_device.return_value = True
        self.is_device_mounted.side_effect = self._fake_is_device_mounted
        with patch_open() as (_open, _file):
            _file.read.return_value = PROC_PARTITIONS
            _file.readlines = MagicMock()
            _file.readlines.return_value = PROC_PARTITIONS.split('\n')
            result = swift_utils.find_block_devices()
        ex = ['/dev/sdb', '/dev/vdb', '/dev/cciss/c1d0']
        self.assertEquals(ex, result)

    def test_find_block_devices_real_world(self):
        self.is_block_device.return_value = True
        side_effect = lambda x: x in ["/dev/sdb", "/dev/sdb1"]
        self.is_device_mounted.side_effect = side_effect
        with patch_open() as (_open, _file):
            _file.read.return_value = REAL_WORLD_PARTITIONS
            _file.readlines = MagicMock()
            _file.readlines.return_value = REAL_WORLD_PARTITIONS.split('\n')
            result = swift_utils.find_block_devices()
        expected = ["/dev/sda"]
        self.assertEquals(expected, result)

    def test_save_script_rc(self):
        self.unit_private_ip.return_value = '10.0.0.1'
        swift_utils.save_script_rc()
        self._save_script_rc.assert_called_with(**SCRIPT_RC_ENV)

    def test_assert_charm_not_supports_ipv6(self):
        self.lsb_release.return_value = {'DISTRIB_ID': 'Ubuntu',
                                         'DISTRIB_RELEASE': '12.04',
                                         'DISTRIB_CODENAME': 'precise',
                                         'DISTRIB_DESCRIPTION': 'Ubuntu 12.04'}
        self.assertRaises(Exception, swift_utils.assert_charm_supports_ipv6)

    def test_assert_charm_supports_ipv6(self):
        self.lsb_release.return_value = {'DISTRIB_ID': 'Ubuntu',
                                         'DISTRIB_RELEASE': '14.04',
                                         'DISTRIB_CODENAME': 'trusty',
                                         'DISTRIB_DESCRIPTION': 'Ubuntu 14.04'}
        swift_utils.assert_charm_supports_ipv6()

    @patch('charmhelpers.contrib.openstack.templating.OSConfigRenderer')
    def test_register_configs_pre_install(self, renderer):
        self.get_os_codename_package.return_value = None
        swift_utils.register_configs()
        renderer.assert_called_with(templates_dir=swift_utils.TEMPLATES,
                                    openstack_release='essex')

    @patch('charmhelpers.contrib.openstack.context.BindHostContext')
    @patch.object(swift_utils, 'SwiftStorageContext')
    @patch.object(swift_utils, 'RsyncContext')
    @patch.object(swift_utils, 'SwiftStorageServerContext')
    @patch('charmhelpers.contrib.openstack.templating.OSConfigRenderer')
    def test_register_configs_post_install(self, renderer,
                                           swift, rsync, server,
                                           bind_context):
        swift.return_value = 'swift_context'
        rsync.return_value = 'rsync_context'
        server.return_value = 'swift_server_context'
        bind_context.return_value = 'bind_host_context'
        self.get_os_codename_package.return_value = 'grizzly'
        configs = MagicMock()
        configs.register = MagicMock()
        renderer.return_value = configs
        swift_utils.register_configs()
        renderer.assert_called_with(templates_dir=swift_utils.TEMPLATES,
                                    openstack_release='grizzly')
        ex = [
            call('/etc/swift/swift.conf', ['swift_server_context']),
            call('/etc/rsync-juju.d/050-swift-storage.conf',
                 ['rsync_context', 'swift_context']),
            call('/etc/swift/account-server.conf', ['swift_context',
                                                    'bind_host_context']),
            call('/etc/swift/object-server.conf', ['swift_context',
                                                   'bind_host_context']),
            call('/etc/swift/container-server.conf', ['swift_context',
                                                      'bind_host_context'])
        ]
        self.assertEquals(ex, configs.register.call_args_list)

    def test_do_upgrade(self):
        self.test_config.set('openstack-origin', 'cloud:precise-grizzly')
        self.get_os_codename_install_source.return_value = 'grizzly'
        swift_utils.do_openstack_upgrade(MagicMock())
        self.configure_installation_source.assert_called_with(
            'cloud:precise-grizzly'
        )
        dpkg_opts = [
            '--option', 'Dpkg::Options::=--force-confnew',
            '--option', 'Dpkg::Options::=--force-confdef',
        ]
        self.assertTrue(self.apt_update.called)
        self.apt_upgrade.assert_called_with(
            options=dpkg_opts,
            fatal=True, dist=True
        )
        services = (swift_utils.ACCOUNT_SVCS + swift_utils.CONTAINER_SVCS +
                    swift_utils.OBJECT_SVCS)
        for service in services:
            self.assertIn(call(service), self.service_restart.call_args_list)
