#!/usr/bin/python3
"""Root-only, reviewed host image deletion. Never prunes volumes or registries."""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

ROOT = Path('/opt/teaching-ai-production')
DOCKER = ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock']
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')
HASH = re.compile(r'[0-9a-f]{64}')
SHA = re.compile(r'[0-9a-f]{40}')
REF = re.compile(r'docker\.cnb\.cool/shukezhinao/edu-agent-cloud-build/(?:api|worker|web|codex-runtime)(?:@sha256:[0-9a-f]{64}|:[0-9a-f]{32})|teaching-ai-production/(?:api|worker):[0-9a-f]{40}')


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(value): return hashlib.sha256(value).hexdigest()


def require(ok, code):
    if not ok: raise ValueError(code)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'RETENTION_DUPLICATE_KEY')
        result[key] = value
    return result


def decode(raw): return json.loads(raw, object_pairs_hook=unique)


class Retention:
    def __init__(self, root=ROOT, *, uid=0, command=None, lock_dir=Path('/run/lock'), clock=time.time):
        self.root, self.uid, self.lock_dir, self.clock = Path(root), uid, Path(lock_dir), clock
        self.ops = self.root/'operations/release'
        self.command = command or self.docker

    def docker(self, arguments):
        # No environment-controlled daemon, config, executable, or shell.
        result = subprocess.run(DOCKER+arguments, check=True, capture_output=True, timeout=60,
                                env={'PATH':'/usr/bin:/bin', 'HOME':'/root', 'LANG':'C.UTF-8'})
        require(len(result.stdout) <= 32*1024**2, 'RETENTION_DOCKER_OUTPUT_LIMIT')
        return result.stdout

    def safe_directory(self, path):
        require(path.resolve() == path and path.is_dir(), 'RETENTION_DIRECTORY_UNSAFE')
        require(path.stat().st_uid == self.uid and not stat.S_IMODE(path.stat().st_mode) & 0o022,
                'RETENTION_DIRECTORY_OWNER')

    def read(self, path, *, limit=4*1024**2):
        require(path.is_relative_to(self.root), 'RETENTION_SCOPE_REFUSED')
        info = path.lstat()
        require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and
                info.st_nlink == 1 and stat.S_IMODE(info.st_mode) in (0o600, 0o400) and info.st_size <= limit,
                'RETENTION_METADATA_UNSAFE')
        return path.read_bytes()

    def write(self, path, value):
        require(path.is_relative_to(self.root), 'RETENTION_SCOPE_REFUSED')
        self.safe_directory(path.parent)
        if path.exists() or path.is_symlink(): self.read(path)
        temporary = path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextlib.contextmanager
    def locks(self):
        handles = []
        try:
            for name in ('teaching-production-release.lock', 'teaching-production-maintenance.lock'):
                fd = os.open(self.lock_dir/name, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600); handles.append(fd)
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1 and
                        stat.S_IMODE(info.st_mode) == 0o600, 'RETENTION_LOCK_UNSAFE')
                try: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise ValueError('RETENTION_CONTROL_BUSY') from None
            yield
        finally:
            for fd in reversed(handles): os.close(fd)

    def policy(self):
        path = self.ops/'image-retention-policy.json'
        value = decode(self.read(path)) if path.exists() or path.is_symlink() else {
            'schemaVersion':1, 'retainSuccessfulPackages':2, 'protectedPackageHashes':[], 'releasedPackageHashes':[]}
        require(isinstance(value, dict) and set(value) == {'schemaVersion','retainSuccessfulPackages',
                'protectedPackageHashes','releasedPackageHashes'} and type(value['schemaVersion']) is int and
                value['schemaVersion'] == 1 and type(value['retainSuccessfulPackages']) is int and
                2 <= value['retainSuccessfulPackages'] <= 20, 'RETENTION_POLICY_REFUSED')
        for name in ('protectedPackageHashes','releasedPackageHashes'):
            rows = value[name]
            require(isinstance(rows, list) and len(rows) <= 20000 and len(set(rows)) == len(rows) and
                    all(isinstance(x,str) and HASH.fullmatch(x) for x in rows), 'RETENTION_POLICY_REFUSED')
        require(not set(value['protectedPackageHashes']) & set(value['releasedPackageHashes']), 'RETENTION_POLICY_CONFLICT')
        return value

    def metadata(self):
        selected = []
        for folder in (self.ops/'state', self.ops/'transfers', self.ops/'incoming', self.root/'releases'):
            if not folder.exists(): continue
            self.safe_directory(folder)
            for directory, subdirs, names in os.walk(folder, followlinks=False):
                base = Path(directory)
                for name in subdirs:
                    require(not (base/name).is_symlink(), 'RETENTION_METADATA_LINK')
                depth=len(base.relative_to(folder).parts)
                # Application files are not deployment metadata. Do not descend into static assets,
                # copied containers, acceptance fixtures, logs, or retention receipts.
                if folder == self.root/'releases':
                    if depth==0:subdirs[:]=[name for name in subdirs if SHA.fullmatch(name)]
                    elif depth==1:subdirs[:]=[name for name in subdirs if HASH.fullmatch(name)]
                    else:subdirs[:]=[]
                elif folder == self.ops/'incoming':
                    if depth>=1:subdirs[:]=[]
                else:subdirs[:]=[]
                for name in names:
                    if (name.endswith('.pending.json') or name in ('manifest.json','deployment-receipt.json',
                        'stage-receipt.json','failure-receipt.json','last-success.json','last-failure.json',
                        'rollback-metadata.json','previous-compose.release.yaml') or folder == self.ops/'transfers'):
                        selected.append(base/name)
                require(len(selected) <= 20000, 'RETENTION_METADATA_LIMIT')
        override = self.ops/'compose.release.yaml'
        if override.exists() or override.is_symlink(): selected.append(override)
        return {str(path.relative_to(self.root)): self.read(path) for path in sorted(set(selected))}

    def snapshot(self):
        self.safe_directory(self.root); self.safe_directory(self.ops)
        ids = self.command(['ps','-aq']).decode().split()
        require(all(re.fullmatch('[0-9a-f]{12,64}', x) for x in ids), 'RETENTION_CONTAINER_IDS')
        containers = decode(self.command(['inspect', *ids])) if ids else []
        image_ids = sorted(set(self.command(['image','ls','--all','--quiet','--no-trunc']).decode().split()))
        require(all(IMAGE.fullmatch(x) for x in image_ids), 'RETENTION_IMAGE_IDS')
        images = decode(self.command(['image','inspect',*image_ids])) if image_ids else []
        require(isinstance(containers,list) and len(containers)==len(ids) and isinstance(images,list) and
                len(images)==len(image_ids), 'RETENTION_INSPECTION_INCOMPLETE')
        clean_containers = []
        for row in containers:
            require(isinstance(row,dict) and re.fullmatch('[0-9a-f]{64}',row.get('Id','')) and
                    IMAGE.fullmatch(row.get('Image','')), 'RETENTION_CONTAINER_INVALID')
            clean_containers.append({'id':row['Id'],'image':row['Image'],
                                     'startedAt':row.get('State',{}).get('StartedAt')})
        require(len({row['id'] for row in clean_containers})==len(ids) and
                all(sum(row['id'].startswith(identifier) for row in clean_containers)==1 for identifier in ids),
                'RETENTION_CONTAINER_SET_CHANGED')
        clean_images = []
        for row in images:
            refs = sorted(set((row.get('RepoTags') or [])+(row.get('RepoDigests') or [])))
            require(IMAGE.fullmatch(row.get('Id','')) and all(isinstance(x,str) for x in refs) and
                    type(row.get('Size',0)) is int and row.get('Size',0)>=0, 'RETENTION_IMAGE_INVALID')
            clean_images.append({'id':row['Id'],'refs':refs,'labels':row.get('Config',{}).get('Labels') or {},
                                 'sizeBytes':row.get('Size',0)})
        require({x['id'] for x in clean_images}==set(image_ids), 'RETENTION_IMAGE_SET_CHANGED')
        raw = self.metadata()
        return {'containers':sorted(clean_containers,key=lambda x:x['id']),
                'images':sorted(clean_images,key=lambda x:x['id']),
                'metadata':{name:digest(value) for name,value in raw.items()}}, raw

    def protect(self, snapshot, raw, policy):
        reasons = {row['id']:[] for row in snapshot['images']}
        references = {ref:row['id'] for row in snapshot['images'] for ref in row['refs']}
        def pin(image, reason):
            if image in reasons and reason not in reasons[image]: reasons[image].append(reason)
        def pin_ref(ref, reason):
            if ref in references: pin(references[ref],reason)
            elif IMAGE.fullmatch(ref): pin(ref,reason)
        for row in snapshot['containers']: pin(row['image'],'container')
        pending = any(name.endswith('.pending.json') for name in raw)
        packages, receipts = {}, {}
        for name, data in raw.items():
            if name.endswith('.yaml'):
                if name.endswith('compose.release.yaml') and '/incoming/' not in '/'+name:
                    for ref in REF.findall(data.decode()): pin_ref(ref,'current-compose')
                continue
            value = decode(data)
            require(isinstance(value,dict),'RETENTION_METADATA_INVALID')
            if name.startswith('releases/'):
                parts = name.split('/')
                require(len(parts)==4 and SHA.fullmatch(parts[1]) and HASH.fullmatch(parts[2]),'RETENTION_PACKAGE_PATH')
                key = parts[2]
                if parts[3]=='manifest.json':
                    require(value.get('sourceSha')==parts[1] and key not in packages,'RETENTION_MANIFEST_IDENTITY')
                    packages[key] = value
                else: receipts.setdefault(key,[]).append((parts[3],value))
        successful = []; unverified = set()
        for key, values in receipts.items():
            for name, value in values:
                if name=='deployment-receipt.json':
                    require(value.get('status')=='deployed' and key in packages,'RETENTION_RECEIPT_INVALID')
                    manifest = packages[key]
                    for field in ('sourceSha','version','sourceRepository'):
                        if field in value and field in manifest:
                            require(value[field]==manifest[field],'RETENTION_RECEIPT_IDENTITY')
                    if 'bundleSha256' in value:
                        require(value['bundleSha256']==key,'RETENTION_RECEIPT_IDENTITY')
                    require(isinstance(value.get('deployedAt'),str),'RETENTION_RECEIPT_INVALID')
                    require(isinstance(value.get('verification'),dict) and value['verification'].get('healthy') is True,'RETENTION_RECEIPT_UNVERIFIED')
                    moment = dt.datetime.fromisoformat(value['deployedAt'].replace('Z','+00:00'))
                    require(moment.tzinfo is not None,'RETENTION_RECEIPT_TIME')
                    unknown_schema = 'schemaVersion' in value and (type(value['schemaVersion']) is not int or value['schemaVersion']!=1)
                    if 'bundleSha256' not in value or unknown_schema:
                        # A recognized historical source in an exact safe package
                        # directory is visible but never proof for deletion.
                        require(value.get('sourceSha')==manifest['sourceSha'],'RETENTION_RECEIPT_IDENTITY')
                        unverified.add(key)
                    else: successful.append((moment,key))
        recent = {key for _,key in sorted(successful,reverse=True)[:policy['retainSuccessfulPackages']]}
        pinned = set(policy['protectedPackageHashes'])|recent|unverified
        released = set(policy['releasedPackageHashes'])
        for name,data in raw.items():
            if name.endswith('.yaml'): continue
            value = decode(data)
            key = value.get('bundleSha256')
            if name.endswith('last-success.json') and isinstance(key,str): pinned.add(key)
            if name.startswith('operations/release/transfers/'):
                if value.get('used') is not True:
                    expires = value.get('expiresAt')
                    try: active = dt.datetime.fromisoformat(expires.replace('Z','+00:00')).timestamp()>self.clock()
                    except (ValueError,AttributeError,TypeError): active = True
                    if active or key not in released: pinned.add(key)
        known = set()
        summaries = []
        for key, manifest in packages.items():
            require(isinstance(manifest.get('images'),dict) and manifest['images'],'RETENTION_MANIFEST_INVALID')
            package_images = []
            for kind,item in manifest['images'].items():
                require(kind in ('api','worker','web','codex-runtime') and isinstance(item,dict), 'RETENTION_MANIFEST_IMAGE')
                ref = item.get('ref',item.get('tag')); image = item.get('id')
                require(isinstance(ref,str) and REF.fullmatch(ref) and isinstance(image,str) and IMAGE.fullmatch(image),
                        'RETENTION_MANIFEST_IMAGE')
                actual = references.get(ref,image)
                known.add(actual); package_images.append(actual)
            unresolved = any(name=='failure-receipt.json' for name,_ in receipts.get(key,[]))
            has_success = any(name=='deployment-receipt.json' for name,_ in receipts.get(key,[]))
            protected = key in pinned or key not in released and (unresolved or not has_success)
            for image in package_images:
                if protected: pin(image,('unverified-receipt:' if key in unverified else 'package:')+key)
            summaries.append({'packageHash':key,'protected':protected,'images':sorted(set(package_images)),
                              'receiptState':'unverified' if key in unverified else 'verified_success' if has_success else 'unconfirmed'})
        # Historical previous overrides remain recovery material until that source's package is released.
        released_sources = {m.get('sourceSha') for key,m in packages.items() if key in released}
        for name,data in raw.items():
            if name.endswith('previous-compose.release.yaml'):
                metadata_name = name.rsplit('/',1)[0]+'/rollback-metadata.json'
                previous = decode(raw[metadata_name]).get('previousSourceSha') if metadata_name in raw else None
                if previous not in released_sources:
                    for ref in REF.findall(data.decode()): pin_ref(ref,'rollback-compose')
        for row in snapshot['images']:
            if row['id'] not in known: pin(row['id'],'unowned')
            if not row['refs'] or any(not REF.fullmatch(ref) for ref in row['refs']): pin(row['id'],'unknown-alias')
        return reasons, summaries, pending

    def make_plan(self):
        policy = self.policy(); snapshot, raw = self.snapshot()
        protected, packages, pending = self.protect(snapshot,raw,policy)
        sizes={row['id']:row['sizeBytes'] for row in snapshot['images']}
        # Docker image sizes include shared layers. This is logical size, never a promised disk saving.
        candidates = [dict(id=row['id'],refs=row['refs'],logicalSizeBytes=row['sizeBytes'])
                      for row in snapshot['images'] if not protected[row['id']]]
        now = self.clock()
        return {'schemaVersion':1,'createdAt':now,'expiresAt':now+600,'policySha256':digest(canonical(policy)),
                'snapshotSha256':digest(canonical(snapshot)),'pending':pending,'images':candidates,
                'protected':[{'id':key,'logicalSizeBytes':sizes[key],'reasons':value} for key,value in sorted(protected.items()) if value],
                'packages':sorted(packages,key=lambda x:x['packageHash'])}

    def apply_plan(self, path, expected_hash):
        require(HASH.fullmatch(expected_hash or ''),'RETENTION_PLAN_HASH')
        raw = self.read(Path(path)); require(digest(raw)==expected_hash,'RETENTION_PLAN_HASH')
        plan = decode(raw)
        require(isinstance(plan,dict) and set(plan)=={'schemaVersion','createdAt','expiresAt','policySha256',
                'snapshotSha256','pending','images','protected','packages'} and type(plan['schemaVersion']) is int and plan['schemaVersion']==1,
                'RETENTION_PLAN_INVALID')
        require(type(plan['createdAt']) in (int,float) and type(plan['expiresAt']) in (int,float) and
                plan['createdAt'] <= self.clock() < plan['expiresAt'] <= plan['createdAt']+600,
                'RETENTION_PLAN_EXPIRED')
        output = self.ops/'state/image-retention'
        output.mkdir(mode=0o700,exist_ok=True); self.safe_directory(output)
        with self.locks():
            fresh = self.make_plan()
            require(not fresh['pending'] and not plan['pending'],'RETENTION_RECOVERY_PENDING')
            for field in ('snapshotSha256','policySha256','images','protected','packages'):
                require(plan[field]==fresh[field],'RETENTION_PLAN_STALE')
            receipt = {'schemaVersion':1,'planSha256':expected_hash,'status':'in_progress','images':[]}
            receipt_path = output/(expected_hash+'.json')
            self.write(receipt_path,receipt)
            for row in plan['images']:
                # Re-read containers immediately before deletion. Docker also refuses references acquired afterwards.
                ids = self.command(['ps','-aq']).decode().split()
                containers = decode(self.command(['inspect',*ids])) if ids else []
                require(isinstance(containers,list) and len(containers)==len(ids) and
                        all(isinstance(x,dict) and isinstance(x.get('Id'),str) and IMAGE.fullmatch(x.get('Image','')) for x in containers) and
                        len({x['Id']for x in containers})==len(ids) and
                        all(sum(x['Id'].startswith(identifier)for x in containers)==1 for identifier in ids),
                        'RETENTION_CONTAINER_SET_CHANGED')
                require(all(x.get('Image')!=row['id'] for x in containers),'RETENTION_IMAGE_NOW_IN_USE')
                current = decode(self.command(['image','inspect',row['id']]))
                require(len(current)==1 and current[0]['Id']==row['id'] and
                        sorted(set((current[0].get('RepoTags') or [])+(current[0].get('RepoDigests') or [])))==row['refs'],
                        'RETENTION_IMAGE_CHANGED')
                receipt['images'].append(dict(id=row['id'],status='deletion_started'))
                self.write(receipt_path,receipt)
                self.command(['image','rm',*row['refs']])
                receipt['images'][-1]['status']='removed'
                self.write(receipt_path,receipt)
            receipt['status']='complete'; self.write(receipt_path,receipt)
            return receipt


def main():
    os.umask(0o077)
    require(os.geteuid()==0,'RETENTION_ROOT_REQUIRED')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('report','apply'))
    parser.add_argument('--plan'); parser.add_argument('--sha256')
    args=parser.parse_args(); target=Retention()
    if args.operation=='report':
        require(not args.sha256,'RETENTION_ARGUMENTS')
        value=target.make_plan()
        if args.plan: target.write(Path(args.plan),value)
        print(canonical(value).decode(),end='')
    else:
        require(bool(args.plan and args.sha256),'RETENTION_ARGUMENTS')
        print(canonical(target.apply_plan(Path(args.plan),args.sha256)).decode(),end='')


if __name__=='__main__':
    try: main()
    except Exception as error:
        # Avoid echoing arbitrary filesystem paths, Docker output, or raw exceptions.
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('RETENTION_[A-Z_]+',str(error)) else 'RETENTION_FAILED'
        print(json.dumps({'status':'failed','code':code})); raise SystemExit(1)
