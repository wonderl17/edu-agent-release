"""Receiver extension: private digest-pinned pulls; never build on Production."""
import datetime as dt
import hashlib
from pathlib import Path
import shutil
import tarfile
import time
import uuid

from cnb_release import canonical, validate_manifest


def validate(c, value, h):
    try:
        validate_manifest(value, h['sourceSha'], h['version'])
    except (ValueError, TypeError, KeyError):
        raise c.Refused('REGISTRY_MANIFEST_REFUSED')
    raw = canonical(value)
    c.require(len(raw) <= 12000 and len(raw) == h['bundleBytes'] and
              hashlib.sha256(raw).hexdigest() == h['bundleSha256'], 'REGISTRY_MANIFEST_HASH_MISMATCH')
    return raw


def prepare(c, h, previous=None):
    c.require(c.ops.fixed_env()['PRODUCTION_ACTIVATED'] == 'true', 'REGISTRY_REQUIRES_ACTIVE_PRODUCTION')
    value = h['registryManifest'] if previous is None else previous['registryManifest']
    validate(c, value, h)
    now = dt.datetime.now(dt.timezone.utc)
    ticket = uuid.uuid4().hex
    record = dict(schemaVersion=1, scope='teaching-production', ticket=ticket,
                  sourceRepository=h['sourceRepository'], sourceSha=h['sourceSha'], version=h['version'],
                  bundleBytes=h['bundleBytes'], bundleSha256=h['bundleSha256'], createdAt=now.isoformat(),
                  expiresAt=(now + dt.timedelta(hours=2)).isoformat(), used=False,
                  transport='registry', registryManifest=value)
    if previous is not None:
        record['previousTransferTicket'] = previous['ticket']
    c.atomic(c.OPS / 'transfers' / (ticket + '.json'), record)
    return dict(status='transfer_resumed' if previous else 'transfer_prepared', transferTicket=ticket,
                bundleBytes=h['bundleBytes'], bundleSha256=h['bundleSha256'], transport='registry')


def ticket(c, h):
    if 'transferTicket' not in h:
        return None
    path = c.OPS / 'transfers' / (h['transferTicket'] + '.json')
    c.private(path)
    record = c.decode(path.read_bytes())
    return record if record.get('transport') == 'registry' else None


def pull(c, h, stream, record):
    raw = validate(c, record.get('registryManifest'), h)
    c.start_liveness(stream)
    c.use_transfer_ticket(h)
    manifest = record['registryManifest']
    c.atomic(c.run_dir / 'manifest.json', raw)
    auth = c.OPS / 'registry-auth'
    c.require(auth.resolve() == auth and auth.stat().st_uid == 0 and auth.stat().st_mode & 0o777 == 0o700,
              'REGISTRY_AUTH_DIRECTORY_UNSAFE')
    c.private(auth / 'config.json')
    docker = c.DOCKER + ['--config', str(auth)]
    proofs, timings = {}, {}
    for kind, item in manifest['images'].items():
        start = time.monotonic()
        c.command(docker + ['pull', '--platform', 'linux/amd64', item['ref']], timeout=1800)
        image = c.decode(c.command(c.DOCKER + ['image', 'inspect', item['ref']]))[0]
        c.require(item['ref'] in image.get('RepoDigests', []) and image.get('Id') == item['id'] and
                  image.get('Os') == 'linux' and image.get('Architecture') == 'amd64' and
                  image.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') == h['sourceSha'] and
                  image.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.version') == h['version'],
                  'REGISTRY_IMAGE_IDENTITY_MISMATCH')
        proofs[kind] = dict(sourceId=item['id'], targetId=image['Id'], registryDigestVerified=True,
                            comparison='registry-digest-config-platform-revision')
        timings[kind] = round(time.monotonic() - start, 3)
    # docker create/cp never executes candidate code or contacts application data.
    container = c.command(c.DOCKER + ['create', manifest['images']['web']['ref']]).decode().strip()
    c.require(len(container) == 64 and all(x in '0123456789abcdef' for x in container), 'WEB_CONTAINER_INVALID')
    copied = c.run_dir / 'web-copied'
    try:
        c.command(c.DOCKER + ['cp', container + ':/usr/share/nginx/html', str(copied)])
    finally:
        c.command(c.DOCKER + ['rm', container])
    try:
        c.web_permissions(copied)  # rejects links and special files before archiving
        paths = sorted(copied.rglob('*'))
        c.require(sum(x.stat().st_size for x in paths if x.is_file()) <= c.MAX_WEB, 'WEB_EXPANSION_LIMIT')
        def metadata(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = int(info.mtime)
            info.pax_headers = {}
            return info
        archive = c.run_dir / 'web.tar.gz'
        with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as out:
            for path in paths:
                out.add(path, arcname=str(path.relative_to(copied)), recursive=False, filter=metadata)
        web = c.run_dir / 'web-dist'
        web_info = c.web_unpack(archive, web, h)
    finally:
        shutil.rmtree(copied, ignore_errors=True)
    c.atomic(c.run_dir / 'registry-timings.json', dict(pullSeconds=timings, buildSeconds=manifest['timings']))
    # Existing migration/activation consumes an image reference named "tag".
    # Set it to the immutable digest; never retag an active source-SHA image.
    runtime = dict(manifest, images={kind: dict(tag=item['ref'], id=item['id'])
                                    for kind, item in manifest['images'].items() if kind != 'web'})
    return runtime, web, web_info, {kind: proof for kind, proof in proofs.items() if kind != 'web'}
