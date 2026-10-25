#!/usr/bin/python3
"""Restricted stdin deployment receiver for the Tencent Production host."""
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from urllib.parse import urlsplit

os.umask(0o077)
ROOT=Path('/opt/teaching-ai-production');OPS=ROOT/'operations/release'
RELEASES=ROOT/'releases';OVERRIDE=OPS/'compose.release.yaml';STATE=OPS/'state'
PROTOCOL='teaching-production-ci-v1';SOURCE='shukezhinao/edu-agent'
MAX_BUNDLE=8*1024**3;MAX_DIAGNOSTICS=5*1024**2;MAX_IMAGES=32*1024**3;MAX_WEB=512*1024**2
MAX_RELAY=5*1024**3
DOCKER=['/usr/bin/docker','--host','unix:///var/run/docker.sock']
phase='protocol';run_dir=None;ops=None
client_done=threading.Event();client_cancelled=threading.Event();client_reason=None;monitor_thread=None;rolling_back=False

class Refused(Exception):pass
def require(ok,code):
 if not ok:raise Refused(code)
def utc():return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z')
def private(path,modes=(0o600,)):
 p=Path(path);s=p.lstat();require(stat.S_ISREG(s.st_mode)and not p.is_symlink()and p.resolve()==p and s.st_uid==0 and s.st_gid==0 and stat.S_IMODE(s.st_mode)in modes,'UNSAFE_PRIVATE_FILE')
def unique_json(pairs):
 result={}
 for k,v in pairs:require(k not in result,'DUPLICATE_JSON_KEY');result[k]=v
 return result
def decode(data):
 try:return json.loads(data,object_pairs_hook=unique_json)
 except Refused:raise
 except Exception:raise Refused('INVALID_JSON')
def atomic(path,value):
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 require(path.parent.resolve()==path.parent and path.parent.stat().st_uid==0,'UNSAFE_STATE_PATH')
 if path.exists()or path.is_symlink():private(path)
 temp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
 fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb')as f:f.write(value if isinstance(value,bytes)else(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode());f.flush();os.fsync(f.fileno())
 os.replace(temp,path)
def header(stream):
 raw=stream.readline(16385);require(len(raw)<=16384 and raw.endswith(b'\n'),'HEADER_LIMIT_OR_TERMINATOR')
 h=decode(raw);require(isinstance(h,dict),'HEADER_OBJECT_REQUIRED')
 action=h.get('action');require(action in('verify','record-attempt','deploy','stage','diagnostics','prepare-transfer','prepare-registry','resume-transfer'),'ACTION_REFUSED')
 expected={'protocol','action','sourceRepository','sourceSha','version'}
 if action in('deploy','stage','diagnostics','prepare-transfer','prepare-registry'):expected|={'bundleBytes','bundleSha256'}
 if action in('deploy','stage'):expected.add('clientKeepsInputOpen')
 if action in('deploy','stage')and'transferTicket'in h:expected.add('transferTicket')
 if action=='resume-transfer':expected.add('previousTransferTicket')
 if action=='prepare-registry':expected.add('registryManifest')
 if action in('deploy','resume-transfer')and'initialGateId'in h:expected.add('initialGateId')
 if action in('deploy','stage','record-attempt','prepare-transfer','prepare-registry','resume-transfer')and('allowSameVersionSourceChange'in h or'expectedCurrentSourceSha'in h):expected|={'allowSameVersionSourceChange','expectedCurrentSourceSha'}
 if action=='diagnostics'and'runId'in h:expected.add('runId')
 require(set(h)==expected and h.get('protocol')==PROTOCOL,'PROTOCOL_OR_FIELDS_REFUSED')
 if action in('deploy','stage'):require(h.get('clientKeepsInputOpen')is True,'CLIENT_LIVENESS_REQUIRED')
 if'transferTicket'in h:require(isinstance(h['transferTicket'],str)and re.fullmatch('[0-9a-f]{32}',h['transferTicket']),'TRANSFER_TICKET_REFUSED')
 if'previousTransferTicket'in h:require(isinstance(h['previousTransferTicket'],str)and re.fullmatch('[0-9a-f]{32}',h['previousTransferTicket']),'TRANSFER_TICKET_REFUSED')
 if'initialGateId'in h:require(isinstance(h['initialGateId'],str)and re.fullmatch('[0-9a-f]{32}',h['initialGateId']),'INITIAL_GATE_ID_REFUSED')
 if'allowSameVersionSourceChange'in h:require(h['allowSameVersionSourceChange']is True and isinstance(h['expectedCurrentSourceSha'],str)and re.fullmatch('[0-9a-f]{40}',h['expectedCurrentSourceSha']),'SAME_VERSION_OVERRIDE_REFUSED')
 require(h.get('sourceRepository')==SOURCE,'SOURCE_REPOSITORY_REFUSED')
 require(isinstance(h.get('sourceSha'),str)and re.fullmatch('[0-9a-f]{40}',h['sourceSha']),'SOURCE_SHA_REFUSED')
 require(isinstance(h.get('version'),str)and len(h['version'])<=80 and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?',h['version']),'VERSION_REFUSED')
 version_main,_,build=h['version'].partition('+');_,has_pre,pre=version_main.partition('-')
 if has_pre:require(all(x and(not x.isdigit()or x=='0'or not x.startswith('0'))for x in pre.split('.')),'SEMVER_PRERELEASE_REFUSED')
 if'+'in h['version']:require(all(build.split('.')),'SEMVER_BUILD_REFUSED')
 if action in('deploy','stage','diagnostics','prepare-transfer','prepare-registry'):
  limit=MAX_BUNDLE if action in('deploy','stage')else MAX_RELAY if action in('prepare-transfer','prepare-registry')else MAX_DIAGNOSTICS
  require(type(h['bundleBytes'])is int and 0<h['bundleBytes']<=limit,'PAYLOAD_SIZE_REFUSED')
  require(isinstance(h['bundleSha256'],str)and re.fullmatch('[0-9a-f]{64}',h['bundleSha256']),'PAYLOAD_HASH_REFUSED')
 if'runId'in h:require(isinstance(h['runId'],str)and re.fullmatch('[0-9]{1,24}',h['runId']),'RUN_ID_REFUSED')
 return h
def hash_file(path):
 h=hashlib.sha256()
 with open(path,'rb')as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def receive(stream,path,size,digest,require_eof=True):
 h=hashlib.sha256();remaining=size
 with path.open('xb')as f:
  while remaining:
   b=stream.read(min(1024*1024,remaining));require(bool(b),'PAYLOAD_TRUNCATED');f.write(b);h.update(b);remaining-=len(b)
  f.flush();os.fsync(f.fileno())
 if require_eof:require(stream.read(1)==b'','PAYLOAD_TRAILING_BYTES')
 require(h.hexdigest()==digest,'PAYLOAD_HASH_MISMATCH')
def client_monitor(stream):
 global client_reason
 while not client_done.is_set():
  try:
   ready,_,_=select.select([stream.fileno()],[],[],0.2)
   if not ready:continue
   data=os.read(stream.fileno(),1)
  except Exception:data=b''
  if client_done.is_set():return
  client_reason='CLIENT_TRAILING_BYTES'if data else'CLIENT_DISCONNECTED'
  client_cancelled.set();os.kill(os.getpid(),signal.SIGUSR1);return
def start_liveness(stream):
 global monitor_thread
 monitor_thread=threading.Thread(target=client_monitor,args=(stream,),daemon=True);monitor_thread.start()
def finish_liveness():
 client_done.set()
 if monitor_thread and monitor_thread is not threading.current_thread():monitor_thread.join(timeout=1)
def regular_member(m):
 require(m.isfile()and m.type in(tarfile.REGTYPE,tarfile.AREGTYPE)and not m.pax_headers,'ARCHIVE_MEMBER_TYPE_REFUSED')
def safe_name(name):
 p=PurePosixPath(name)
 require(bool(name)and'\x00'not in name and not name.startswith(('/', './'))and all(x not in('..','.')for x in name.split('/'))and str(p)==name.rstrip('/'),'ARCHIVE_PATH_REFUSED')
 return p
def unpack_bundle(path,destination):
 allowed={'manifest.json','images.tar.zst','web.tar.gz'};found=set()
 with path.open('rb')as archive:
  for m in tar_members(archive):
   regular_member(m);require(m.name in allowed and m.name not in found,'BUNDLE_MEMBER_REFUSED');found.add(m.name)
   require(0<m.size<=(1024**2 if m.name=='manifest.json'else MAX_BUNDLE),'BUNDLE_MEMBER_SIZE_REFUSED')
   with(destination/m.name).open('xb')as out:copy_exact(archive,out,m.size)
 require(found==allowed,'BUNDLE_FILES_MISSING')
def copy_exact(source,destination,size):
 remaining=size
 while remaining:
  block=source.read(min(1024*1024,remaining));require(bool(block),'ARCHIVE_TRUNCATED');destination.write(block);remaining-=len(block)
def tar_members(stream):
 # Parse physical headers ourselves: reject GNU/PAX extensions before tarfile
 # can allocate an attacker-sized extended metadata header.
 while True:
  block=stream.read(512);require(len(block)==512,'ARCHIVE_TRUNCATED')
  if not any(block):
   tail=stream.read(10241);require(len(tail)<=10240 and not any(tail),'ARCHIVE_TRAILING_CONTENT');return
  try:member=tarfile.TarInfo.frombuf(block,'utf-8','strict')
  except Exception:raise Refused('ARCHIVE_HEADER_INVALID')
  require(member.type in(tarfile.REGTYPE,tarfile.AREGTYPE,tarfile.DIRTYPE),'ARCHIVE_MEMBER_TYPE_REFUSED')
  require(member.size>=0,'ARCHIVE_MEMBER_SIZE_REFUSED')
  require(not member.isdir()or member.size==0,'ARCHIVE_DIRECTORY_SIZE_REFUSED')
  yield member
  padding=(-member.size)%512
  if padding:require(stream.read(padding)==b'\0'*padding,'ARCHIVE_PADDING_INVALID')
def manifest(path,h):
 m=decode(path.read_bytes());require(isinstance(m,dict)and set(m)=={'schemaVersion','deployment','sourceRepository','sourceSha','version','images','files'},'MANIFEST_FIELDS_REFUSED')
 require(type(m['schemaVersion'])is int and m['schemaVersion']==1 and m['deployment']=='teaching-production'and all(m[k]==h[k]for k in('sourceRepository','sourceSha','version')),'MANIFEST_IDENTITY_MISMATCH')
 require(set(m['images'])=={'api','worker'}and set(m['files'])=={'images.tar.zst','web.tar.gz'},'MANIFEST_ARTIFACT_SET_REFUSED')
 for kind,item in m['images'].items():
  require(isinstance(item,dict)and set(item)=={'tag','id'}and item['tag']=='teaching-ai-production/'+kind+':'+h['sourceSha']and isinstance(item['id'],str)and re.fullmatch('sha256:[0-9a-f]{64}',item['id']),'MANIFEST_IMAGE_REFUSED')
 for name,item in m['files'].items():
  require(isinstance(item,dict)and set(item)=={'bytes','sha256'}and type(item['bytes'])is int and item['bytes']>0 and isinstance(item['sha256'],str)and re.fullmatch('[0-9a-f]{64}',item['sha256']),'MANIFEST_FILE_METADATA_REFUSED')
  f=path.parent/name;require(f.stat().st_size==item['bytes']and hash_file(f)==item['sha256'],'MANIFEST_FILE_HASH_MISMATCH')
 return m
def web_unpack(path,destination,h):
 destination.mkdir(mode=0o755);seen=set();total=0
 with gzip.open(path,mode='rb')as archive:
  for item in tar_members(archive):
   p=safe_name(item.name);require(item.name not in seen,'WEB_DUPLICATE_MEMBER');seen.add(item.name)
   require(not item.pax_headers and(item.isdir()or item.isfile()and item.type in(tarfile.REGTYPE,tarfile.AREGTYPE)),'WEB_MEMBER_TYPE_REFUSED')
   require(len(seen)<=20000,'WEB_MEMBER_COUNT_REFUSED');total+=item.size;require(total<=MAX_WEB,'WEB_EXPANSION_LIMIT')
   target=destination/str(p)
   if item.isdir():target.mkdir(mode=0o755,parents=True,exist_ok=True);continue
   target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
   with target.open('xb')as out:copy_exact(archive,out,item.size)
   target.chmod(0o644)
 require((destination/'index.html').is_file()and(destination/'version.json').is_file(),'WEB_ENTRYPOINT_MISSING')
 version=decode((destination/'version.json').read_bytes());require(version.get('version')==h['version']and version.get('buildSha')==h['sourceSha'],'WEB_VERSION_MISMATCH')
 scripts=list((destination/'assets').glob('http-*.js'));require(0<len(scripts)<=4,'WEB_HTTP_ASSET_MISSING')
 good=False
 for script in scripts:
  text=script.read_text();require(script.stat().st_size<=4*1024**2,'WEB_HTTP_ASSET_TOO_LARGE')
  # Inspect the configured-base resolver adjacent to the actual desktop API
  # transport. Other assets legitimately link to Production desktop downloads.
  position=text.find('getApiBaseUrl');require(position>=0,'WEB_HTTP_TRANSPORT_MISSING')
  prefix=text[max(0,position-1200):position]
  if re.search(r'return\s*[\w$]+\([`\"\']/api[`\"\']\s*,',prefix):good=True
 require(good,'WEB_API_BASE_NOT_RELATIVE')
 environment_proved=False
 for script in (destination/'assets').glob('*.js'):
  if script.stat().st_size>16*1024**2:continue
  source=script.read_text()
  pattern=r'Object\.freeze\(\{environment:([`"\'])shukezhinao-prod\1,mode:([`"\'])production\2,buildSha:([`"\'])'+re.escape(h['sourceSha'])+r'\3,'
  if re.search(pattern,source):environment_proved=True
 require(environment_proved,'WEB_PRODUCTION_ENVIRONMENT_MISMATCH')
 web_permissions(destination)
 return{'files':len(seen),'bytes':total,'apiBase':'/api','version':version['version'],'buildSha':version['buildSha']}
def web_permissions(destination):
 # Explicit chmod is required: receiver umask 077 intentionally protects all
 # other files, but nginx's unprivileged worker must traverse static bind dirs.
 require(destination.resolve()==destination and not destination.is_symlink(),'WEB_TREE_UNSAFE')
 paths=[destination];directories=[];files=[]
 for parent,dirs,names in os.walk(destination,followlinks=False):
  paths.extend(Path(parent)/name for name in dirs+names)
  require(len(paths)<=20001,'WEB_MEMBER_COUNT_REFUSED')
 for path in paths:
  info=path.lstat();require(info.st_uid==os.geteuid()and not path.is_symlink(),'WEB_TREE_UNSAFE')
  if stat.S_ISDIR(info.st_mode):directories.append(path)
  else:
   require(stat.S_ISREG(info.st_mode)and info.st_nlink==1,'WEB_TREE_UNSAFE');files.append(path)
 require(destination in directories,'WEB_TREE_UNSAFE')
 for path in directories:path.chmod(0o755)
 for path in files:path.chmod(0o644)
def log(data):
 if run_dir:
  with(run_dir/'private.log').open('ab')as f:f.write(data if isinstance(data,bytes)else data.encode())
def command(args,timeout=120,data=None):
 if client_cancelled.is_set()and not rolling_back:raise Refused(client_reason or'CLIENT_DISCONNECTED')
 process=subprocess.Popen(args,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,env=ops.fixed_env()if ops else{'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LANG':'C.UTF-8'})
 try:
  stdout,stderr=process.communicate(input=data,timeout=timeout)
 except BaseException:
  try:os.killpg(process.pid,signal.SIGTERM)
  except ProcessLookupError:pass
  try:stdout,stderr=process.communicate(timeout=15)
  except subprocess.TimeoutExpired:
   try:os.killpg(process.pid,signal.SIGKILL)
   except ProcessLookupError:pass
   stdout,stderr=process.communicate(timeout=5)
  if stderr:log(stderr)
  raise
 if stderr:log(stderr)
 require(process.returncode==0,'PRIVATE_COMMAND_FAILED');return stdout
def images_unpack(path,m,h):
 expanded=path.parent/'images.tar'
 with expanded.open('xb')as out:
  error=(path.parent/'private.log').open('ab')
  process=subprocess.Popen(['/usr/bin/zstd','-d','-c','--',str(path)],stdout=subprocess.PIPE,stderr=error)
  size=0
  try:
   for block in iter(lambda:process.stdout.read(1024*1024),b''):
    size+=len(block);require(size<=MAX_IMAGES,'IMAGE_EXPANSION_LIMIT');out.write(block)
   require(process.wait()==0,'IMAGE_DECOMPRESSION_FAILED')
  finally:
   if process.poll()is None:process.kill();process.wait()
   error.close()
 expected={v['tag']:v for v in m['images'].values()}
 with expanded.open('rb')as archive:
  members={}
  for member in tar_members(archive):
   safe_name(member.name);require(member.name not in members,'IMAGE_DUPLICATE_MEMBER');require(len(members)<20000,'IMAGE_MEMBER_COUNT_REFUSED')
   require(member.isdir()or member.isfile(),'IMAGE_MEMBER_TYPE_REFUSED')
   members[member.name]=(archive.tell(),member.size,member.isfile());archive.seek(member.size,1)
  def entry_bytes(name,limit):
   require(name in members,'IMAGE_MEMBER_MISSING');offset,size,regular=members[name];require(regular and 0<size<=limit,'IMAGE_METADATA_LIMIT');archive.seek(offset);value=archive.read(size);require(len(value)==size,'IMAGE_METADATA_TRUNCATED');return value
  entries=decode(entry_bytes('manifest.json',128*1024));require(isinstance(entries,list)and len(entries)==2,'IMAGE_MANIFEST_SET_REFUSED')
  actual=set();configs={}
  for entry in entries:
   tags=entry.get('RepoTags');require(isinstance(tags,list)and len(tags)==1 and tags[0]in expected,'IMAGE_TAG_SET_REFUSED');actual.add(tags[0])
   data=entry_bytes(entry['Config'],1024**2);require('sha256:'+hashlib.sha256(data).hexdigest()==expected[tags[0]]['id'],'IMAGE_CONFIG_ID_MISMATCH')
   value=decode(data);require(value.get('os')=='linux'and value.get('architecture')=='amd64'and value.get('config',{}).get('Labels',{}).get('org.opencontainers.image.revision')==h['sourceSha'],'IMAGE_REVISION_OR_PLATFORM_MISMATCH');configs[tags[0]]=value
  require(actual==set(expected),'IMAGE_TAG_SET_MISSING')
  if'index.json'in members:
   index=decode(entry_bytes('index.json',128*1024));require(len(index.get('manifests',[]))==2,'OCI_INDEX_SET_REFUSED')
   index_tags=set()
   for descriptor in index['manifests']:
    annotations=descriptor.get('annotations',{});tag=annotations.get('io.containerd.image.name')or annotations.get('org.opencontainers.image.ref.name')
    require(isinstance(tag,str)and tag.removeprefix('docker.io/')in expected,'OCI_INDEX_TAG_REFUSED');tag=tag.removeprefix('docker.io/')
    require(tag not in index_tags,'OCI_INDEX_DUPLICATE_TAG');index_tags.add(tag)
    digest=descriptor.get('digest');require(isinstance(digest,str)and re.fullmatch('sha256:[0-9a-f]{64}',digest),'OCI_MANIFEST_DIGEST_REFUSED')
    raw=entry_bytes('blobs/sha256/'+digest[7:],128*1024);require('sha256:'+hashlib.sha256(raw).hexdigest()==digest,'OCI_MANIFEST_HASH_MISMATCH')
    oci_manifest=decode(raw);require(oci_manifest.get('config',{}).get('digest')==expected[tag]['id'],'OCI_CONFIG_ID_MISMATCH')
    raw_config=entry_bytes('blobs/sha256/'+expected[tag]['id'][7:],1024**2);require('sha256:'+hashlib.sha256(raw_config).hexdigest()==expected[tag]['id'],'OCI_CONFIG_BLOB_HASH_MISMATCH')
   require(index_tags==set(expected),'OCI_INDEX_TAG_SET_MISSING')
 return expanded,configs
def image_equivalence(loaded,config,item,h):
 require(loaded.get('Os')==config.get('os')=='linux'and loaded.get('Architecture')==config.get('architecture')=='amd64','LOADED_IMAGE_PLATFORM_MISMATCH')
 require(loaded.get('Config')==config.get('config'),'LOADED_IMAGE_CONFIG_MISMATCH')
 require(config.get('rootfs',{}).get('type')=='layers'and loaded.get('RootFS',{}).get('Type')=='layers'and loaded.get('RootFS',{}).get('Layers')==config.get('rootfs',{}).get('diff_ids'),'LOADED_IMAGE_ROOTFS_MISMATCH')
 require(loaded['Config'].get('Labels',{}).get('org.opencontainers.image.revision')==h['sourceSha'],'LOADED_IMAGE_REVISION_MISMATCH')
 require(isinstance(loaded.get('Id'),str)and re.fullmatch('sha256:[0-9a-f]{64}',loaded['Id']),'LOADED_IMAGE_ID_INVALID')
 return{'sourceId':item['id'],'targetId':loaded['Id'],'configRootfsVerified':True,'normalizationVerified':loaded['Id']!=item['id'],'comparison':'complete-config-rootfs-platform-revision'}
def existing_image_policy(existing,config,item,h,before):
 try:
  image_equivalence(existing,config,item,h);return{'inactiveTagRebound':False}
 except Refused:
  tag=item['tag'];old_id=existing.get('Id')
  require(re.fullmatch(r'teaching-ai-production/(api|worker):'+re.escape(h['sourceSha']),tag)is not None,'INACTIVE_IMAGE_TAG_SCOPE_REFUSED')
  require(isinstance(old_id,str)and re.fullmatch('sha256:[0-9a-f]{64}',old_id),'INACTIVE_IMAGE_ID_INVALID')
  require(((existing.get('Config')or{}).get('Labels')or{}).get('org.opencontainers.image.revision')==h['sourceSha'],'INACTIVE_IMAGE_REVISION_MISMATCH')
  require(any(value.removeprefix('docker.io/')==tag for value in existing.get('RepoTags',[])if isinstance(value,str)),'INACTIVE_IMAGE_TAG_SCOPE_REFUSED')
  for container in before.values():
   reference=container.get('Config',{}).get('Image','')
   require(reference.removeprefix('docker.io/')!=tag and container.get('Image')!=old_id,'ACTIVE_IMAGE_TAG_COLLISION')
  # docker load may repoint this unused tag, but no old image ID is deleted.
  return{'inactiveTagRebound':True,'previousTargetId':old_id}
def artifact_directory(h):
 RELEASES.mkdir(mode=0o700,exist_ok=True);require(RELEASES.resolve()==RELEASES and RELEASES.stat().st_uid==os.geteuid(),'RELEASE_DIRECTORY_UNSAFE');source=RELEASES/h['sourceSha']
 require(not source.is_symlink(),'RELEASE_DIRECTORY_UNSAFE');source.mkdir(mode=0o700,exist_ok=True)
 require(source.resolve()==source and source.is_dir()and source.stat().st_uid==os.geteuid(),'RELEASE_DIRECTORY_UNSAFE')
 return source/h['bundleSha256']
@contextlib.contextmanager
def release_lock():
 fd=os.open('/run/lock/teaching-production-release.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
 s=os.fstat(fd);require(s.st_uid==0 and stat.S_ISREG(s.st_mode)and stat.S_IMODE(s.st_mode)==0o600,'RELEASE_LOCK_UNSAFE')
 try:
  try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:raise Refused('ANOTHER_RELEASE_ACTIVE')
  yield
 finally:os.close(fd)
def initialize():
 global ops
 require(os.geteuid()==0 and ROOT.resolve()==ROOT and OPS.resolve()==OPS and ROOT.stat().st_uid==0,'TARGET_SCOPE_REFUSED')
 private(OPS/'receiver.py');private(ROOT/'operations/startup/production-ops.py')
 spec=importlib.util.spec_from_file_location('production_ops',ROOT/'operations/startup/production-ops.py');ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)
 ops.fixed_env()
def current():
 ops.preflight();containers=ops.inspect();web=containers['web'];mounts=[x for x in web['Mounts']if x['Destination']=='/usr/share/nginx/html']
 require(len(mounts)==1 and mounts[0]['Type']=='bind'and mounts[0]['RW']is False,'CURRENT_WEB_MOUNT_REFUSED')
 source=Path(mounts[0]['Source']);require(source.resolve()==source and source.is_relative_to(ROOT),'CURRENT_WEB_SOURCE_REFUSED')
 value=decode((source/'version.json').read_bytes());require(isinstance(value.get('version'),str)and re.fullmatch('[0-9a-f]{40}',value.get('buildSha','')),'CURRENT_WEB_VERSION_INVALID')
 return value,containers
def read_state(name):
 path=STATE/name
 if not path.exists():return{}
 private(path);return decode(path.read_bytes())
def version_compare(left,right):
 def parts(version):
  main,_,pre=version.split('+',1)[0].partition('-');return tuple(int(x)for x in main.split('.')),pre.split('.')if pre else[]
 lc,lp=parts(left);rc,rp=parts(right)
 if lc!=rc:return 1 if lc>rc else-1
 if not lp or not rp:return (1 if not lp else-1)if lp!=rp else 0
 for a,b in zip(lp,rp):
  if a==b:continue
  if a.isdigit()and b.isdigit():return 1 if int(a)>int(b)else-1
  if a.isdigit()!=b.isdigit():return-1 if a.isdigit()else 1
  return 1 if a>b else-1
 return (len(lp)>len(rp))-(len(lp)<len(rp))
def version_policy(h,current):
 comparison=version_compare(h['version'],current['version']);require(comparison>=0,'VERSION_REGRESSION_REFUSED')
 if h.get('allowSameVersionSourceChange'):
  require(h['version']==current['version']and h.get('expectedCurrentSourceSha')==current['buildSha'],'SAME_VERSION_CURRENT_SOURCE_CHANGED')
 require(comparison>0 or h['sourceSha']==current['buildSha']or h.get('allowSameVersionSourceChange')is True,'VERSION_SOURCE_COLLISION')
def initial_handler():
 path=OPS/'initial-activation.py';private(path)
 spec=importlib.util.spec_from_file_location('production_initial_activation',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module,module.Context(globals())
def verify(h):
 if ops.fixed_env()['PRODUCTION_ACTIVATED']=='false':
  ready=ops.bootstrap_ready();ready['initialActivationReady']=False
  if(ROOT/'operations/cutover/initial-activation.gate.json').exists():
   try:
    handler,context=initial_handler();gate=handler.gate(context)
    ready.update(initialActivationReady=True,initialGateId=gate['gateId'],initialBundleBytes=gate['bundleBytes'],initialBundleSha256=gate['bundleSha256'])
   except Exception as error:ready['initialActivationCode']=str(error)if isinstance(error,Refused)else'INITIAL_GATE_INVALID'
  return{'status':'verified',**ready,'lastAttemptedVersion':ready['version']}
 value,_=current();attempt=read_state('last-attempt.json')
 try:healthy=ops.snapshot().get('healthy')is True
 except Exception:raise Refused('TARGET_UNHEALTHY')
 require(healthy,'TARGET_UNHEALTHY')
 return{'status':'verified','version':value['version'],'currentSourceSha':value['buildSha'],'lastAttemptedVersion':attempt.get('version',value['version']),'configValid':True,'healthy':True}
def record(h):
 value,_=current();old=read_state('last-attempt.json')
 version_policy(h,value)
 require(version_compare(h['version'],old.get('version',value['version']))>=0,'VERSION_REGRESSION_REFUSED')
 atomic(STATE/'last-attempt.json',{'version':h['version'],'sourceSha':h['sourceSha'],'sourceRepository':SOURCE,'attemptedAt':utc(),'status':'attempt_recorded'})
 return{'status':'attempt_recorded','version':h['version'],'sourceSha':h['sourceSha']}

def transfer_helper(action,payload):
 path=OPS/'transfer.py';private(path,(0o600,0o700))
 result=decode(command(['/usr/bin/python3',str(path),action],timeout=180 if action=='prepare'else 1800,data=json.dumps(payload,separators=(',',':')).encode()))
 require(isinstance(result,dict),'TRANSFER_HELPER_RESPONSE_INVALID');return result
def transfer_payload(h,ticket):
 return{'ticket':ticket,'sourceSha':h['sourceSha'],'version':h['version'],'bytes':h['bundleBytes'],'sha256':h['bundleSha256']}
def ticket_expiry(value):
 require(isinstance(value,str),'TRANSFER_EXPIRY_INVALID')
 try:expiry=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
 except Exception:raise Refused('TRANSFER_EXPIRY_INVALID')
 require(expiry.tzinfo is not None,'TRANSFER_EXPIRY_INVALID');return expiry
def registry_handler():
 import types
 path=OPS/'tencent_registry.py';private(path);private(OPS/'cnb_release.py')
 spec=importlib.util.spec_from_file_location('tencent_registry',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module,types.SimpleNamespace(**globals())
def prepare_transfer(h):
 ticket=uuid.uuid4().hex;result=transfer_helper('prepare',transfer_payload(h,ticket));expiry=ticket_expiry(result.get('expiresAt'));moment=dt.datetime.now(dt.timezone.utc)
 require(dt.timedelta(0)<expiry-moment<=dt.timedelta(hours=2),'TRANSFER_EXPIRY_OUT_OF_RANGE')
 require(result.get('status')=='prepared'and result.get('uploadMethod')=='PUT','TRANSFER_HELPER_RESPONSE_INVALID')
 headers=result.get('uploadHeaders');require(isinstance(headers,dict)and set(headers)=={'Content-Type','Content-Length'}and headers['Content-Type']=='application/octet-stream'and str(headers['Content-Length'])==str(h['bundleBytes']),'TRANSFER_UPLOAD_HEADERS_REFUSED')
 url=result.get('uploadUrl');require(isinstance(url,str)and len(url)<=16384,'TRANSFER_UPLOAD_URL_REFUSED');parsed=urlsplit(url)
 hosts={'teaching-production-releases-1331158445.cos.accelerate.myqcloud.com','teaching-production-releases-1331158445.cos.ap-beijing.myqcloud.com'}
 require(parsed.scheme=='https'and parsed.hostname in hosts and parsed.port in(None,443)and not parsed.username and not parsed.password and not parsed.fragment and parsed.query and parsed.path=='/teaching-production/releases/'+h['sourceSha']+'/'+ticket+'.tar','TRANSFER_UPLOAD_URL_REFUSED')
 record={'schemaVersion':1,'scope':'teaching-production','ticket':ticket,'sourceRepository':SOURCE,'sourceSha':h['sourceSha'],'version':h['version'],'bundleBytes':h['bundleBytes'],'bundleSha256':h['bundleSha256'],'createdAt':moment.isoformat(),'expiresAt':expiry.isoformat(),'used':False}
 require(not(OPS/'transfers'/(ticket+'.json')).exists(),'TRANSFER_TICKET_COLLISION')
 atomic(OPS/'transfers'/(ticket+'.json'),record)
 # The signed URL exists only in this response, not in tickets or raw logs.
 return{'status':'transfer_prepared','transferTicket':ticket,'uploadUrl':url,'expiresAt':result['expiresAt'],'uploadMethod':'PUT','uploadHeaders':headers}
def resume_transfer(h):
 bootstrap=ops.fixed_env()['PRODUCTION_ACTIVATED']=='false'
 if bootstrap:
  ready=ops.bootstrap_ready();require(version_compare(h['version'],ready['version'])>=0,'VERSION_REGRESSION_REFUSED')
  require(not h.get('allowSameVersionSourceChange'),'BOOTSTRAP_OVERRIDE_REFUSED')
 else:
  require('initialGateId'not in h,'INITIAL_GATE_ON_ACTIVE_RUNTIME_REFUSED');value,_=current();version_policy(h,value)
 previous=h['previousTransferTicket'];path=OPS/'transfers'/(previous+'.json')
 require(path.exists(),'TRANSFER_TICKET_NOT_FOUND');private(path);record=decode(path.read_bytes())
 require(record.get('schemaVersion')==1 and record.get('scope')=='teaching-production'and record.get('ticket')==previous and record.get('sourceRepository')==SOURCE,'TRANSFER_TICKET_IDENTITY_MISMATCH')
 require(all(record.get(k)==h[k]for k in('sourceSha','version'))and type(record.get('used'))is bool,'TRANSFER_TICKET_IDENTITY_MISMATCH')
 require(type(record.get('bundleBytes'))is int and 0<record['bundleBytes']<=MAX_RELAY and isinstance(record.get('bundleSha256'),str)and re.fullmatch('[0-9a-f]{64}',record['bundleSha256']),'TRANSFER_TICKET_IDENTITY_MISMATCH')
 if bootstrap:
  handler,context=initial_handler();handler.gate(context,{**h,'bundleBytes':record['bundleBytes'],'bundleSha256':record['bundleSha256']})
 else:require('initialGateId'not in record,'INITIAL_GATE_ON_ACTIVE_RUNTIME_REFUSED')
 if record.get('transport')=='registry':
  handler,context=registry_handler();return handler.prepare(context,{**h,'bundleBytes':record['bundleBytes'],'bundleSha256':record['bundleSha256']},record)
 moment=dt.datetime.now(dt.timezone.utc);created=ticket_expiry(record.get('createdAt'));expiry=ticket_expiry(record.get('expiresAt'));origin=ticket_expiry(record.get('objectCreatedAt',record.get('createdAt')))
 require(origin<=created<=moment and dt.timedelta(0)<expiry-created<=dt.timedelta(hours=2)and moment-origin<dt.timedelta(days=1),'TRANSFER_RESUME_EXPIRED')
 ticket=uuid.uuid4().hex;payload={'ticket':ticket,'previousTicket':previous,'sourceSha':h['sourceSha'],'version':h['version'],'bytes':record['bundleBytes'],'sha256':record['bundleSha256']}
 result=transfer_helper('copy',payload);completed=dt.datetime.now(dt.timezone.utc);new_expiry=ticket_expiry(result.get('expiresAt'))
 require(result.get('status')=='copied'and result.get('bytes')==record['bundleBytes']and result.get('sha256')==record['bundleSha256']and result.get('bodyHashVerified')is False,'TRANSFER_COPY_RECEIPT_MISMATCH')
 require(dt.timedelta(0)<new_expiry-completed<=dt.timedelta(hours=1),'TRANSFER_EXPIRY_OUT_OF_RANGE')
 new_expiry=min(new_expiry,completed+dt.timedelta(hours=1),origin+dt.timedelta(days=1))
 require(new_expiry>completed,'TRANSFER_RESUME_EXPIRED')
 fresh={'schemaVersion':1,'scope':'teaching-production','ticket':ticket,'sourceRepository':SOURCE,'sourceSha':h['sourceSha'],'version':h['version'],'bundleBytes':record['bundleBytes'],'bundleSha256':record['bundleSha256'],'createdAt':completed.isoformat(),'expiresAt':new_expiry.isoformat(),'objectCreatedAt':origin.isoformat(),'previousTransferTicket':previous,'used':False}
 if bootstrap:fresh['initialGateId']=h['initialGateId']
 require(not(OPS/'transfers'/(ticket+'.json')).exists(),'TRANSFER_TICKET_COLLISION');atomic(OPS/'transfers'/(ticket+'.json'),fresh)
 # Copy metadata is not a body proof: deploy still downloads and hashes it.
 return{'status':'transfer_resumed','transferTicket':ticket,'bundleBytes':record['bundleBytes'],'bundleSha256':record['bundleSha256']}
def use_transfer_ticket(h):
 ticket=h['transferTicket'];path=OPS/'transfers'/(ticket+'.json');require(path.exists(),'TRANSFER_TICKET_NOT_FOUND');private(path);record=decode(path.read_bytes())
 require(record.get('schemaVersion')==1 and record.get('scope')=='teaching-production'and record.get('ticket')==ticket and record.get('sourceRepository')==SOURCE,'TRANSFER_TICKET_IDENTITY_MISMATCH')
 require(all(record.get(k)==h[k]for k in('sourceSha','version','bundleBytes','bundleSha256')),'TRANSFER_TICKET_IDENTITY_MISMATCH')
 require(record.get('initialGateId')==h.get('initialGateId'),'INITIAL_GATE_TICKET_MISMATCH')
 require(record.get('used')is False,'TRANSFER_TICKET_ALREADY_USED')
 expiry=ticket_expiry(record.get('expiresAt'));created=ticket_expiry(record.get('createdAt'));moment=dt.datetime.now(dt.timezone.utc)
 require(created<=moment<expiry and dt.timedelta(0)<expiry-created<=dt.timedelta(hours=2),'TRANSFER_TICKET_EXPIRED')
 record['used']=True;record['usedAt']=utc();atomic(path,record)
 return transfer_payload(h,ticket)
def relay_download(h,bundle):
 payload=use_transfer_ticket(h);payload['outputPath']=str(bundle)
 result=transfer_helper('download',payload)
 require(result.get('status')=='downloaded'and result.get('bytes')==h['bundleBytes']and result.get('sha256')==h['bundleSha256'],'TRANSFER_DOWNLOAD_RECEIPT_MISMATCH')
 private(bundle);require(bundle.stat().st_size==h['bundleBytes']and hash_file(bundle)==h['bundleSha256'],'TRANSFER_DOWNLOAD_HASH_MISMATCH')
def drain(general):
 deadline=time.monotonic()+900
 while True:
  output=command(DOCKER+['exec','-i',general['Id'],'node','--input-type=module'],timeout=25,data=(OPS/'queue-probe.mjs').read_bytes())
  value=decode(output)
  if value.get('idle'):return value
  require(time.monotonic()<deadline,'ACTIVE_WORK_DRAIN_TIMEOUT');time.sleep(5)
def candidate_override(h,m,web):
 return{'services':{'api':{'image':m['images']['api']['tag']},'worker-general':{'image':m['images']['worker']['tag']},'worker-ppt':{'image':m['images']['worker']['tag']},'web':{'volumes':[{'type':'bind','source':str(web),'target':'/usr/share/nginx/html','read_only':True,'bind':{'create_host_path':True}}]}}}
def up_four():
 command(ops.compose_argv()+['up','-d','--no-deps','--no-build','--pull','never','--force-recreate','--wait','--wait-timeout','180','api','worker-general','worker-ppt','web'],timeout=300)
def migration(h,m):
 env=ops.config()['services']['api']['environment'];ops.isolation(env,'api')
 require('\n'not in env['DATABASE_URL']and'\r'not in env['DATABASE_URL'],'MIGRATION_DATABASE_URL_INVALID')
 filename=run_dir/'migration.env';atomic(filename,('DATABASE_URL='+env['DATABASE_URL']+'\nNODE_ENV=production\nPRISMA_MIGRATE_DEPLOY=false\nPRISMA_DB_PUSH=false\n').encode())
 name='teaching-production-migrate-'+h['sourceSha'][:12]+'-'+uuid.uuid4().hex[:8]
 try:command(DOCKER+['run','--rm','--name',name,'--network','teaching-production_app','--env-file',str(filename),'--entrypoint','/usr/local/bin/pnpm',m['images']['api']['tag'],'exec','prisma','migrate','deploy','--schema','/app/prisma/schema.prisma'],timeout=600)
 finally:
  filename.unlink(missing_ok=True)
  subprocess.run(DOCKER+['rm','--force',name],capture_output=True,timeout=30,env=ops.fixed_env())
def postflight(h,m,before,image_proofs):
 deadline=time.monotonic()+180
 while True:
  sample=ops.snapshot()
  if sample['healthy']:break
  require(time.monotonic()<deadline,'RELEASE_HEALTH_TIMEOUT');time.sleep(4)
 value,after=current();require(value['version']==h['version']and value['buildSha']==h['sourceSha'],'RUNNING_WEB_VERSION_MISMATCH')
 for name,kind in [('api','api'),('worker-general','worker'),('worker-ppt','worker')]:require(after[name]['Image']==image_proofs[kind]['targetId'],'RUNNING_IMAGE_ID_MISMATCH')
 for name in('gateway','postgres','redis','storage'):require(after[name]['Id']==before[name]['Id']and after[name]['State']['StartedAt']==before[name]['State']['StartedAt'],'IMMUTABLE_SERVICE_CHANGED')
 response=decode(command(['/usr/bin/curl','--silent','--show-error','--fail','--max-time','10','http://127.0.0.1:28080/version.json'],timeout=15));require(response.get('version')==h['version']and response.get('buildSha')==h['sourceSha'],'PUBLIC_WEB_VERSION_MISMATCH')
 return{'healthy':True,'version':h['version'],'sourceSha':h['sourceSha'],'workerConsumers':[x['workers']for x in sample['dependencies']['consumers']],'images':image_proofs,'immutableServicesPreserved':4,'modelsInvokedByReceiver':0}
def deploy(h,stream):
 global run_dir,phase,rolling_back
 if ops.fixed_env()['PRODUCTION_ACTIVATED']=='false':
  handler,context=initial_handler();return handler.deploy(context,h,stream)
 require('initialGateId'not in h,'INITIAL_GATE_ON_ACTIVE_RUNTIME_REFUSED')
 value,before=current();version_policy(h,value)
 staging=OPS/'incoming';staging.mkdir(mode=0o700,exist_ok=True);run_dir=staging/(h['sourceSha']+'-'+uuid.uuid4().hex[:12]);run_dir.mkdir(mode=0o700)
 require(shutil.disk_usage(OPS).free>=MAX_IMAGES+h['bundleBytes']+MAX_WEB+2*1024**3,'INSUFFICIENT_RELEASE_DISK')
 handler,context=registry_handler();registry=handler.ticket(context,h)
 if registry:
  phase='registry-pull';m,web,web_info,image_proofs=handler.pull(context,h,stream,registry)
 else:
  phase='receive';bundle=run_dir/'bundle.tar'
  if'transferTicket'in h:
   start_liveness(stream);phase='relay-download';relay_download(h,bundle)
  else:
   receive(stream,bundle,h['bundleBytes'],h['bundleSha256'],require_eof=False);start_liveness(stream)
  unpack_bundle(bundle,run_dir);m=manifest(run_dir/'manifest.json',h)
  phase='validate-artifacts';web=run_dir/'web-dist';web_info=web_unpack(run_dir/'web.tar.gz',web,h);expanded,archive_configs=images_unpack(run_dir/'images.tar.zst',m,h)
  phase='load-images';existing_policies={}
  for item in m['images'].values():
   existing=subprocess.run(DOCKER+['image','inspect',item['tag']],capture_output=True,timeout=30,env=ops.fixed_env())
   if existing.returncode==0:existing_policies[item['tag']]=existing_image_policy(decode(existing.stdout)[0],archive_configs[item['tag']],item,h,before)
  command(DOCKER+['load','--input',str(expanded)],timeout=900)
  image_proofs={}
  for kind,item in m['images'].items():
   image=decode(command(DOCKER+['image','inspect',item['tag']]))[0];image_proofs[kind]={**image_equivalence(image,archive_configs[item['tag']],item,h),**existing_policies.get(item['tag'],{'inactiveTagRebound':False})}
 # Daily recovery is owned by the Mac operator; releases do not capture or upload backups.
 snapshot_id=None
 target=artifact_directory(h)
 if target.exists():
  require(target.resolve()==target and target.is_dir()and(target/'manifest.json').read_bytes()==(run_dir/'manifest.json').read_bytes(),'RELEASE_DIRECTORY_COLLISION')
  web_permissions(target/'web-dist')
  shutil.rmtree(web)
 else:
  target.mkdir(mode=0o700);shutil.move(str(web),target/'web-dist');atomic(target/'manifest.json',(run_dir/'manifest.json').read_bytes())
 previous=OVERRIDE.read_bytes()if OVERRIDE.exists()else None
 atomic(run_dir/'rollback-metadata.json',{'previousOverridePresent':previous is not None,'previousSourceSha':value['buildSha'],'snapshotId':snapshot_id})
 if previous is not None:atomic(run_dir/'previous-compose.release.yaml',previous)
 stopped=False;switched=False;migration_started=False;migration_ok=False
 with ops.maintenance('ci-release-'+h['sourceSha'][:12],minutes=45):
  try:
   phase='drain';stopped=True;command(DOCKER+['stop','--time','30',before['api']['Id']],timeout=45);drain(before['worker-general'])
   command(DOCKER+['stop','--time','45',before['worker-general']['Id'],before['worker-ppt']['Id']],timeout=100)
   phase='migration';migration_started=True;migration(h,m);migration_ok=True
   phase='activate';switched=True;atomic(OVERRIDE,candidate_override(h,m,target/'web-dist'));ops.preflight();up_four()
   phase='verify';verification=postflight(h,m,before,image_proofs)
   finish_liveness()
   result={'status':'deployed','version':h['version'],'sourceSha':h['sourceSha'],'snapshotId':snapshot_id,'backupPolicy':'mac-direct-daily-no-pre-release-backup','verification':verification,'deployedAt':utc(),'sourceRepository':SOURCE}
   atomic(STATE/'last-success.json',result);atomic(target/'deployment-receipt.json',result)
  except Exception as error:
   rolling_back=True
   phase_before_rollback=phase;rollback_ok=True
   try:
    if switched:
     if previous is None:OVERRIDE.unlink(missing_ok=True)
     else:atomic(OVERRIDE,previous)
     up_four()
    elif stopped:command(DOCKER+['start',before['api']['Id'],before['worker-general']['Id'],before['worker-ppt']['Id']],timeout=60)
   except Exception:rollback_ok=False
   result={'status':'failed','code':'DEPLOY_FAILED_ROLLBACK_RESTORED'if rollback_ok else'DEPLOY_FAILED_ROLLBACK_INCOMPLETE','causeCode':str(error)if isinstance(error,Refused)else type(error).__name__,'phase':phase_before_rollback,'version':h['version'],'sourceSha':h['sourceSha'],'snapshotId':snapshot_id,'configurationRollbackSucceeded':rollback_ok,'databaseAutomaticallyRestored':False,'migrationStarted':migration_started,'migrationSucceeded':migration_ok,'manualDatabaseReviewRequired':migration_started,'failedAt':utc()}
   atomic(STATE/'last-failure.json',result);atomic(run_dir/'failure-receipt.json',result);return result
 return result
def stage(h,stream):
 global run_dir,phase
 require('transferTicket'in h,'STAGE_RELAY_REQUIRED')
 if ops.fixed_env()['PRODUCTION_ACTIVATED']=='false':
  ready=ops.bootstrap_ready();require(version_compare(h['version'],ready['version'])>=0,'VERSION_REGRESSION_REFUSED');require(not h.get('allowSameVersionSourceChange'),'BOOTSTRAP_OVERRIDE_REFUSED')
  before=ops.bootstrap_containers()
 else:
  value,before=current();version_policy(h,value)
 staging=OPS/'incoming';staging.mkdir(mode=0o700,exist_ok=True);run_dir=staging/(h['sourceSha']+'-'+uuid.uuid4().hex[:12]);run_dir.mkdir(mode=0o700)
 require(shutil.disk_usage(OPS).free>=MAX_IMAGES+h['bundleBytes']+MAX_WEB+2*1024**3,'INSUFFICIENT_RELEASE_DISK')
 handler,context=registry_handler();registry=handler.ticket(context,h)
 if registry:
  phase='stage-registry-pull';m,web,web_info,proofs=handler.pull(context,h,stream,registry)
 else:
  phase='stage-relay-download';start_liveness(stream);bundle=run_dir/'bundle.tar';relay_download(h,bundle)
  phase='stage-validate';unpack_bundle(bundle,run_dir);m=manifest(run_dir/'manifest.json',h);web=run_dir/'web-dist';web_info=web_unpack(run_dir/'web.tar.gz',web,h);expanded,configs=images_unpack(run_dir/'images.tar.zst',m,h)
  phase='stage-load-images'
  for item in m['images'].values():
   existing=subprocess.run(DOCKER+['image','inspect',item['tag']],capture_output=True,timeout=30,env=ops.fixed_env())
   if existing.returncode==0:existing_image_policy(decode(existing.stdout)[0],configs[item['tag']],item,h,before)
  command(DOCKER+['load','--input',str(expanded)],timeout=900);proofs={}
  for kind,item in m['images'].items():proofs[kind]=image_equivalence(decode(command(DOCKER+['image','inspect',item['tag']]))[0],configs[item['tag']],item,h)
 target=artifact_directory(h)
 if target.exists():
  require(target.resolve()==target and target.is_dir()and(target/'manifest.json').read_bytes()==(run_dir/'manifest.json').read_bytes(),'RELEASE_DIRECTORY_COLLISION');web_permissions(target/'web-dist');shutil.rmtree(web)
 else:
  target.mkdir(mode=0o700);shutil.move(str(web),target/'web-dist');atomic(target/'manifest.json',(run_dir/'manifest.json').read_bytes())
 result={'status':'staged','sourceSha':h['sourceSha'],'version':h['version'],'transferTicket':h['transferTicket'],'bundleBytes':h['bundleBytes'],'bundleSha256':h['bundleSha256']}
 if(run_dir/'registry-timings.json').exists():result['timings']=decode((run_dir/'registry-timings.json').read_bytes())
 atomic(target/'stage-receipt.json',{**result,'images':proofs,'web':web_info,'stagedAt':utc(),'appDeployments':0,'databaseMigrations':0,'servicesModified':0});finish_liveness();return result
def diagnostics(h,stream):
 destination=OPS/'diagnostics'/h['sourceSha'];destination.mkdir(mode=0o700,parents=True,exist_ok=True)
 require(destination.resolve()==destination and destination.stat().st_uid==0,'DIAGNOSTICS_PATH_UNSAFE')
 name=(h.get('runId')or dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))+'.log';temp=destination/(name+'.tmp-'+uuid.uuid4().hex)
 try:
  receive(stream,temp,h['bundleBytes'],h['bundleSha256']);target=destination/name
  require(not target.exists()and not target.is_symlink(),'DIAGNOSTICS_ALREADY_EXISTS');os.replace(temp,target)
 finally:temp.unlink(missing_ok=True)
 return{'status':'diagnostics_saved','bytes':h['bundleBytes'],'sha256':h['bundleSha256']}
def main():
 global phase
 require(len(sys.argv)==1,'ARGUMENTS_REFUSED');stream=os.fdopen(os.dup(sys.stdin.fileno()),'rb',buffering=0)
 try:
  signal.alarm(45);h=header(stream);initialize();signal.alarm(10800 if h['action']in('deploy','stage')else 1830 if h['action']=='resume-transfer'else 180 if h['action']=='prepare-transfer'else 120 if h['action']=='diagnostics'else 45)
  if h['action']=='diagnostics':return diagnostics(h,stream)
  if h['action']=='verify':require(stream.read(1)==b'','PAYLOAD_UNEXPECTED');return verify(h)
  if h['action']=='prepare-registry':
   require(stream.read(1)==b'','PAYLOAD_UNEXPECTED');handler,context=registry_handler();return handler.prepare(context,h)
  if h['action']=='prepare-transfer':require(stream.read(1)==b'','PAYLOAD_UNEXPECTED');return prepare_transfer(h)
  if h['action']=='resume-transfer':
   require(stream.read(1)==b'','PAYLOAD_UNEXPECTED')
   with release_lock():return resume_transfer(h)
  if h['action']=='record-attempt':
   require(stream.read(1)==b'','PAYLOAD_UNEXPECTED')
   with release_lock():return record(h)
  if h['action']=='stage':
   with release_lock():return stage(h,stream)
  with release_lock():return deploy(h,stream)
 finally:finish_liveness();stream.close()
def alarm(_signum,_frame):raise Refused('RECEIVER_DEADLINE_EXCEEDED')
signal.signal(signal.SIGALRM,alarm)
def interrupted(_signum,_frame):raise Refused('RECEIVER_INTERRUPTED')
for _signal in(signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(_signal,interrupted)
def client_interrupt(_signum,_frame):raise Refused(client_reason or'CLIENT_DISCONNECTED')
signal.signal(signal.SIGUSR1,client_interrupt)
if __name__=='__main__':
 try:
  result=main();print(json.dumps(result,separators=(',',':')));sys.exit(1 if result.get('status')=='failed'else 0)
 except Exception as error:
  if run_dir:log(('receiver failure phase='+phase+' code='+(str(error)if isinstance(error,Refused)else type(error).__name__)+'\n').encode())
  print(json.dumps({'status':'failed','code':str(error)if isinstance(error,Refused)else'RECEIVER_FAILED','phase':phase},separators=(',',':')));sys.exit(1)
 finally:
  if run_dir and run_dir.parent==OPS/'incoming'and run_dir.resolve()==run_dir:
   for name in('bundle.tar','bundle.tar.cos-partial','images.tar','images.tar.zst','web.tar.gz'):(run_dir/name).unlink(missing_ok=True)
   if(run_dir/'web-dist').is_dir():shutil.rmtree(run_dir/'web-dist')
