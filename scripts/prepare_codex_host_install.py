"""Prepare a local, hash-bound host installer bundle. Never contacts a host."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer_contract',REPO/'server/codex_host_install.py')
contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)


def prepare(audit_dir,output):
    raw=(audit_dir/'installation-precondition.json').read_bytes();audit=contract.decode(raw)
    contract.require(audit['schemaVersion']==1 and audit['host']=='49.233.166.202'and audit['root']==str(contract.ROOT)and
                     audit['productionChanged']is False,'HOST_AUDIT_SCOPE')
    expected={}
    for name in set(contract.FILES)|contract.DEPENDENCIES|{contract.SETTINGS}:
        row=audit['files'][name]
        if row['exists']:
            contract.require(row['uid']==0 and row['gid']==0 and row['mode']=='0o600','HOST_AUDIT_OWNERSHIP')
            expected[name]=row['sha256']
            if name!=contract.SETTINGS:
                contract.require(contract.digest((audit_dir/Path(name).name).read_bytes())==row['sha256'],'HOST_AUDIT_LOCAL_BYTES_CHANGED')
        else:expected[name]=None
    contract.require(all(expected[n]for n in contract.DEPENDENCIES|{contract.STARTUP,contract.RECEIVER}), 'HOST_AUDIT_BASE_REQUIRED')
    patch=(REPO/'server/production-ops-codex.patch').read_bytes()
    with tempfile.TemporaryDirectory(prefix='codex-host-bundle-')as directory:
        source=Path(directory)/'production-ops.py';source.write_bytes((audit_dir/'production-ops.py').read_bytes())
        subprocess.run(['patch','--batch','--fuzz=0',str(source)],input=patch,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
        payload={contract.STARTUP:source.read_bytes(),contract.RECEIVER:(REPO/'server/tencent_receiver.py').read_bytes(),
                 'operations/release/cnb_release.py':(REPO/'scripts/cnb_release.py').read_bytes(),
                 'operations/release/tencent_registry.py':(REPO/'scripts/tencent_registry.py').read_bytes(),
                 'operations/release/codex_runtime.py':(REPO/'server/codex_runtime.py').read_bytes(),
                 'operations/release/codex_configure.py':(REPO/'server/codex_configure.py').read_bytes()}
    for name,data in payload.items():compile(data,name,'exec')
    installer=(REPO/'server/codex_host_install.py').read_bytes()
    manifest=dict(schemaVersion=1,scope=contract.SCOPE,root=str(contract.ROOT),
        files={name:dict(beforeSha256=expected[name],afterSha256=contract.digest(payload[name]),payload='files/'+filename)for name,filename in contract.FILES.items()},
        dependencies={name:expected[name]for name in sorted(contract.DEPENDENCIES)},settingsBeforeSha256=expected[contract.SETTINGS],
        installerSha256=contract.digest(installer),sourceAuditSha256=contract.digest(raw),startupPatchSha256=contract.digest(patch))
    data=contract.canonical(manifest);identity=contract.digest(data)
    destination=output/identity;destination.mkdir(mode=0o700,parents=True,exist_ok=False)
    (destination/'files').mkdir(mode=0o700)
    for name,content in {'manifest.json':data,'install.py':installer,
                         **{'files/'+contract.FILES[n]:v for n,v in payload.items()}}.items():
        fd=os.open(destination/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb')as stream:stream.write(content)
    return dict(status='prepared',bundleSha256=identity,path=str(destination),files=len(payload),
                productionChanged=False,containsCredentials=False,requiresFreshTargetPreflight=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--audit-dir',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(prepare(args.audit_dir.resolve(),args.output_dir.resolve())))


if __name__=='__main__':main()
