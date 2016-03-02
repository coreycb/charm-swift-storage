import os
import re
import shutil
import tempfile

from subprocess import check_call, call, CalledProcessError, check_output

# Stuff copied from cinder py charm, needs to go somewhere
# common.
from misc_utils import (
    ensure_block_device,
    clean_storage,
    is_paused
)

from swift_storage_context import (
    SwiftStorageContext,
    SwiftStorageServerContext,
    RsyncContext,
)

from charmhelpers.fetch import (
    apt_upgrade,
    apt_update
)

from charmhelpers.core.host import (
    mkdir,
    mount,
    fstab_add,
    service_restart,
    lsb_release
)

from charmhelpers.core.hookenv import (
    config,
    log,
    DEBUG,
    INFO,
    ERROR,
    unit_private_ip,
)

from charmhelpers.contrib.storage.linux.utils import (
    is_block_device,
    is_device_mounted,
)

from charmhelpers.contrib.storage.linux.loopback import (
    is_mapped_loopback_device,
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    get_os_codename_install_source,
    get_os_codename_package,
    save_script_rc as _save_script_rc,
)

from charmhelpers.contrib.openstack import (
    templating,
    context
)

from charmhelpers.core.decorators import (
    retry_on_exception,
)

PACKAGES = [
    'swift', 'swift-account', 'swift-container', 'swift-object',
    'xfsprogs', 'gdisk', 'lvm2', 'python-jinja2', 'python-psutil',
]

TEMPLATES = 'templates/'

REQUIRED_INTERFACES = {
    'proxy': ['swift-storage'],
}

ACCOUNT_SVCS = [
    'swift-account', 'swift-account-auditor',
    'swift-account-reaper', 'swift-account-replicator'
]

CONTAINER_SVCS = [
    'swift-container', 'swift-container-auditor',
    'swift-container-updater', 'swift-container-replicator',
    'swift-container-sync'
]

OBJECT_SVCS = [
    'swift-object', 'swift-object-auditor',
    'swift-object-updater', 'swift-object-replicator'
]

SWIFT_SVCS = ACCOUNT_SVCS + CONTAINER_SVCS + OBJECT_SVCS

RESTART_MAP = {
    '/etc/rsync-juju.d/050-swift-storage.conf': ['rsync'],
    '/etc/swift/account-server.conf': ACCOUNT_SVCS,
    '/etc/swift/container-server.conf': CONTAINER_SVCS,
    '/etc/swift/object-server.conf': OBJECT_SVCS,
    '/etc/swift/swift.conf': ACCOUNT_SVCS + CONTAINER_SVCS + OBJECT_SVCS
}

SWIFT_CONF_DIR = '/etc/swift'
SWIFT_RING_EXT = 'ring.gz'


def ensure_swift_directories():
    '''
    Ensure all directories required for a swift storage node exist with
    correct permissions.
    '''
    dirs = [
        SWIFT_CONF_DIR,
        '/var/cache/swift',
        '/srv/node',
    ]
    [mkdir(d, owner='swift', group='swift') for d in dirs
     if not os.path.isdir(d)]


def register_configs():
    release = get_os_codename_package('python-swift', fatal=False) or 'essex'
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    configs.register('/etc/swift/swift.conf',
                     [SwiftStorageContext()])
    configs.register('/etc/rsync-juju.d/050-swift-storage.conf',
                     [RsyncContext(), SwiftStorageServerContext()])
    for server in ['account', 'object', 'container']:
        configs.register('/etc/swift/%s-server.conf' % server,
                         [SwiftStorageServerContext(),
                          context.BindHostContext(),
                          context.WorkerConfigContext()]),
    return configs


def swift_init(target, action, fatal=False):
    '''
    Call swift-init on a specific target with given action, potentially
    raising exception.
    '''
    cmd = ['swift-init', target, action]
    if fatal:
        return check_call(cmd)
    return call(cmd)


def do_openstack_upgrade(configs):
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)

    log('Performing OpenStack upgrade to %s.' % (new_os_rel))
    configure_installation_source(new_src)
    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_update()
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    configs.set_release(openstack_release=new_os_rel)
    configs.write_all()
    if not is_paused():
        for service in SWIFT_SVCS:
            service_restart(service)


def _is_storage_ready(partition):
    """
    A small helper to determine if a given device is suitabe to be used as
    a storage device.
    """
    return is_block_device(partition) and not is_device_mounted(partition)


def get_mount_point(device):
    mnt_point = None
    try:
        out = check_output(['findmnt', device])
        mnt_points = []
        for line in out.split('\n'):
            if line and not line.startswith('TARGET'):
                mnt_points.append(line.split()[0])
        if len(mnt_points) > 1:
            log('Device {} mounted in multiple times, ignoring'.format(device))
        else:
            mnt_point = mnt_points[0]
    except CalledProcessError:
        # findmnt returns non-zero rc if dev not mounted
        pass
    return mnt_point


def find_block_devices(include_mounted=False):
    found = []
    incl = ['sd[a-z]', 'vd[a-z]', 'cciss\/c[0-9]d[0-9]']

    with open('/proc/partitions') as proc:
        partitions = [p.split() for p in proc.readlines()[2:]]
    for partition in [p[3] for p in partitions if p]:
        for inc in incl:
            _re = re.compile(r'^(%s)$' % inc)
            if _re.match(partition):
                found.append(os.path.join('/dev', partition))
    if include_mounted:
        devs = [f for f in found if is_block_device(f)]
    else:
        devs = [f for f in found if _is_storage_ready(f)]
    return devs


def guess_block_devices():
    bdevs = find_block_devices(include_mounted=True)
    gdevs = []
    for dev in bdevs:
        if is_device_mounted(dev):
            mnt_point = get_mount_point(dev)
            if mnt_point and mnt_point.startswith('/srv/node'):
                gdevs.append(dev)
        else:
            gdevs.append(dev)
    return gdevs


def determine_block_devices():
    block_device = config('block-device')

    if not block_device or block_device in ['None', 'none']:
        log('No storage devices specified in config as block-device',
            level=ERROR)
        return None

    if block_device == 'guess':
        bdevs = guess_block_devices()
    else:
        bdevs = block_device.split(' ')

    # attempt to ensure block devices, but filter out missing devs
    _none = ['None', 'none', None]
    valid_bdevs = \
        [x for x in map(ensure_block_device, bdevs) if x not in _none]
    log('Valid ensured block devices: %s' % valid_bdevs)
    return valid_bdevs


def mkfs_xfs(bdev):
    cmd = ['mkfs.xfs', '-f', '-i', 'size=1024', bdev]
    check_call(cmd)


def setup_storage():
    # Ensure /srv/node exists just in case no disks
    # are detected and used.
    mkdir(os.path.join('/srv', 'node'),
          owner='swift', group='swift',
          perms=0o755)
    for dev in determine_block_devices():
        if config('overwrite') in ['True', 'true']:
            clean_storage(dev)
        # if not cleaned and in use, mkfs should fail.
        mkfs_xfs(dev)
        _dev = os.path.basename(dev)
        _mp = os.path.join('/srv', 'node', _dev)
        mkdir(_mp, owner='swift', group='swift')

        options = None
        loopback_device = is_mapped_loopback_device(dev)

        if loopback_device:
            dev = loopback_device
            options = "loop, defaults"

        mountpoint = '/srv/node/%s' % _dev
        filesystem = "xfs"

        mount(dev, mountpoint, filesystem=filesystem)
        fstab_add(dev, mountpoint, filesystem, options=options)

    check_call(['chown', '-R', 'swift:swift', '/srv/node/'])
    check_call(['chmod', '-R', '0755', '/srv/node/'])


@retry_on_exception(3, base_delay=2, exc_type=CalledProcessError)
def fetch_swift_rings(rings_url):
    """Fetch rings from leader proxy unit.

    Note that we support a number of retries if a fetch fails since we may
    have hit the very small update window on the proxy side.
    """
    log('Fetching swift rings from proxy @ %s.' % rings_url, level=INFO)
    target = SWIFT_CONF_DIR
    tmpdir = tempfile.mkdtemp(prefix='swiftrings')
    try:
        synced = []
        for server in ['account', 'object', 'container']:
            url = '%s/%s.%s' % (rings_url, server, SWIFT_RING_EXT)
            log('Fetching %s.' % url, level=DEBUG)
            ring = '%s.%s' % (server, SWIFT_RING_EXT)
            cmd = ['wget', url, '--retry-connrefused', '-t', '10', '-O',
                   os.path.join(tmpdir, ring)]
            check_call(cmd)
            synced.append(ring)

        # Once all have been successfully downloaded, move them to actual
        # location.
        for f in synced:
            os.rename(os.path.join(tmpdir, f), os.path.join(target, f))
    finally:
        shutil.rmtree(tmpdir)


def save_script_rc():
    env_vars = {}
    ip = unit_private_ip()
    for server in ['account', 'container', 'object']:
        port = config('%s-server-port' % server)
        url = 'http://%s:%s/recon/diskusage|"mounted":true' % (ip, port)
        svc = server.upper()
        env_vars.update({
            'OPENSTACK_PORT_%s' % svc: port,
            'OPENSTACK_SWIFT_SERVICE_%s' % svc: '%s-server' % server,
            'OPENSTACK_URL_%s' % svc: url,
        })
    _save_script_rc(**env_vars)


def assert_charm_supports_ipv6():
    """Check whether we are able to support charms ipv6."""
    if lsb_release()['DISTRIB_CODENAME'].lower() < "trusty":
        raise Exception("IPv6 is not supported in the charms for Ubuntu "
                        "versions less than Trusty 14.04")


def concat_rsync_fragments():
    log('Concatenating rsyncd.d fragments')
    rsyncd_dir = '/etc/rsyncd.d'
    rsyncd_conf = ""
    for filename in sorted(os.listdir(rsyncd_dir)):
        with open(os.path.join(rsyncd_dir, filename), 'r') as fragment:
            rsyncd_conf += fragment.read()
    with open('/etc/rsyncd.conf', 'w') as f:
        f.write(rsyncd_conf)


def setup_rsync():
    '''
    Ensure all directories required for rsync exist with correct permissions.
    '''
    root_dirs = [
        '/etc/rsync-juju.d',
    ]
    [mkdir(d, owner='root', group='root') for d in root_dirs
     if not os.path.isdir(d)]

    rsyncd_base = """uid = nobody
gid = nogroup
pid file = /var/run/rsyncd.pid
syslog facility = daemon
socket options = SO_KEEPALIVE

&include /etc/rsync-juju.d
"""

    f = open('/etc/rsyncd.conf', 'w')
    f.write(rsyncd_base)
    f.close()


def assess_status(configs):
    """Assess status of current unit"""
    if is_paused():
        return ("maintenance",
                "Paused. Use 'resume' action to resume normal service.")
    else:
        return ("active", "Unit is ready")
