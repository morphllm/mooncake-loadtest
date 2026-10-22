#!/usr/bin/env python3
"""Parallel SSH coordinator for the canonical storage qualification pipeline.

Plan locally by default; plan --remote validates on nodes without erasing disks.
wheel.file names an existing REMOTE file. No keys, credentials or wheels are copied.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time

from qualify_node import normalize_node

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}')
SSH_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@:-]{0,253}')
REMOTE_BASE = '/opt/mooncake-qualification'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def load_config(path, complete):
    value = json.loads(Path(path).read_text())
    if value.get('schema') != 1 or not isinstance(value.get('run_id'), str) or not NAME.fullmatch(value['run_id']):
        raise ValueError('fleet needs schema: 1 and a path-safe run_id of at most 48 characters')
    nodes = value.get('nodes')
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('fleet nodes must be a nonempty array')
    result = []
    for node in nodes:
        node = normalize_node(node, complete=complete)
        if not SSH_NAME.fullmatch(node['ssh']):
            raise ValueError('ssh must be an existing SSH alias or user@host, without options')
        node.update(schema=1, run_id=value['run_id'])
        result.append(node)
    if len({n['name'] for n in result}) != len(result) or len({n['ssh'] for n in result}) != len(result):
        raise ValueError('node names and SSH targets must be unique')
    return dict(schema=1, run_id=value['run_id'], nodes=result)


def source_snapshot(root=ROOT, require_clean=True):
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(git('status', '--porcelain', '--untracked-files=all'))
    if dirty and require_clean:
        raise RuntimeError('canonical checkout has uncommitted files; commit and review before remote staging')
    files = git('ls-files').splitlines()
    selected = [p for p in files if p == 'mooncake-loadtest.sh' or (p.startswith('scripts/') and p.endswith('.py'))]
    if not {'mooncake-loadtest.sh', 'scripts/qualify_node.py', 'scripts/fleet.py'}.issubset(selected):
        raise RuntimeError('qualification scripts must be committed in the canonical checkout')
    data = {}
    for name in selected:
        path = root / name
        if path.is_symlink(): raise RuntimeError(f'bundle symlink excluded: {name}')
        data[name] = subprocess.run(['git', '-C', str(root), 'show', f'HEAD:{name}'], capture_output=True, check=True).stdout
    return dict(commit=git('rev-parse', 'HEAD'), dirty=dirty, files=data)


def bundle(snapshot, node):
    files = dict(snapshot['files'], **{'node.json': json_bytes(node)})
    manifest = dict(schema=1, source_commit=snapshot['commit'], files={name: sha(data) for name, data in files.items()})
    files['bundle.json'] = json_bytes(manifest)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, data in sorted(files.items()):
            entry = tarfile.TarInfo(name); entry.size = len(data); entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(data))
    payload = gzip.compress(output.getvalue(), mtime=0)
    return payload, sha(payload), manifest


def remote_path(node, action='run'):
    stage = action if action in ('inventory', 'plan') else 'run'
    return f"{REMOTE_BASE}/{node['run_id']}/{node['name']}/{stage}"


def unit_name(node):
    identity = f"{node['run_id']}/{node['name']}"
    return 'mooncake-qual-' + sha(identity.encode())[:24]


def node_command(node, action, resume=False):
    base = remote_path(node, action)
    command = ['python3', f'{base}/scripts/qualify_node.py', '--config', f'{base}/node.json', '--out', f'{base}/artifacts']
    if action == 'run': command += ['--apply', '--erase-data']
    elif action == 'inventory': command += ['--inventory-only']
    elif action == 'cleanup': command += ['--cleanup']
    else: command += ['--plan']
    if resume or action == 'cleanup': command += ['--resume']
    return command


class SSH:
    def __init__(self, node):
        self.node = node

    def call(self, argv, data=None, timeout=120, binary=False):
        root_command = (['sudo', '-n'] if self.node['sudo'] else []) + list(argv)
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                   '--', self.node['ssh'], shlex.join(root_command)]
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{self.node['name']}: SSH/root command exit {result.returncode}: {result.stderr.decode(errors='replace')[-2000:]}")
        return result.stdout if binary else result.stdout.decode()

    def preflight(self):
        self.call(['sh', '-c', 'test "$(id -u)" = 0 && command -v python3 && command -v systemd-run'])


STAGE = r'''
import hashlib,io,json,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1]); digest=sys.argv[2]; resume=sys.argv[3]=='1'; data=sys.stdin.buffer.read()
if hashlib.sha256(data).hexdigest()!=digest: raise SystemExit('bundle transfer SHA256 mismatch')
with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
 members=archive.getmembers()
 if any(not m.isfile() or pathlib.PurePosixPath(m.name).is_absolute() or '..' in pathlib.PurePosixPath(m.name).parts for m in members):
  raise SystemExit('invalid bundle members')
 files={m.name:archive.extractfile(m).read() for m in members}
 manifest=json.loads(files['bundle.json'])
 if len(files)!=len(members) or set(files)!=set(manifest['files'])|{'bundle.json'}: raise SystemExit('bundle manifest membership mismatch')
 for name,want in manifest['files'].items():
  if hashlib.sha256(files[name]).hexdigest()!=want: raise SystemExit('bundle file checksum mismatch: '+name)
 if root.exists():
  if not resume: raise SystemExit('owned run directory already exists; use --resume')
  if (root/'bundle.sha256').read_text().strip()!=digest: raise SystemExit('resume bundle changed')
  for name,content in files.items():
   target=root/name
   if target.is_symlink() or target.read_bytes()!=content: raise SystemExit('staged file changed: '+name)
 else:
  root.mkdir(parents=True,mode=0o700)
  for name,content in files.items():
   target=root/name; target.parent.mkdir(parents=True,exist_ok=True)
   with target.open('xb') as stream: stream.write(content)
   target.chmod(0o600)
  (root/'bundle.sha256').write_text(digest+'\n')
print(json.dumps({'path':str(root),'bundle_sha256':digest,'source_commit':manifest['source_commit']}))
'''

STATUS = r'''
import json,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]); state=root/'artifacts/state.json'
unit=subprocess.run(['systemctl','show',sys.argv[2],'-p','ActiveState','-p','SubState','-p','ExecMainStatus','-p','Result'],capture_output=True,text=True)
print(json.dumps({'state':json.loads(state.read_text()) if state.exists() else None,'missing_state':not state.exists(),
 'unit':dict(line.split('=',1) for line in unit.stdout.splitlines() if '=' in line),'unit_query_returncode':unit.returncode}))
'''

COLLECT = r'''
import gzip,hashlib,io,json,pathlib,sys,tarfile
root=pathlib.Path(sys.argv[1]); artifacts=root/'artifacts'
if not artifacts.is_dir(): raise SystemExit('missing artifacts directory')
# Read regular artifact files only; runtime, wheel payloads and credentials are outside this subtree.
files=sorted(p for p in artifacts.rglob('*') if p.is_file() and not p.is_symlink())
if any(p.suffix=='.whl' or p.name in ('id_rsa','id_ed25519','authorized_keys') for p in files): raise SystemExit('private payload unexpectedly in artifacts')
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:
 hashes={}
 for path in files:
  content=path.read_bytes(); name=str(path.relative_to(root)); hashes[name]=hashlib.sha256(content).hexdigest()
  item=tarfile.TarInfo(name); item.size=len(content); item.mode=0o600; archive.addfile(item,io.BytesIO(content))
 receipt=json.dumps(hashes,sort_keys=True,indent=2).encode(); item=tarfile.TarInfo('ARTIFACT_SHA256.json'); item.size=len(receipt)
 archive.addfile(item,io.BytesIO(receipt))
'''


def launch(ssh, node, action, resume):
    current = json.loads(ssh.call(['python3', '-c', STATUS, remote_path(node), unit_name(node)]))
    if current['unit'].get('ActiveState') in ('active', 'activating', 'deactivating'):
        if resume: return dict(status='already_running', observation=current)
        raise RuntimeError('owned qualification unit is already running')
    if resume and current['unit'].get('ActiveState') == 'failed':
        ssh.call(['systemctl', 'reset-failed', unit_name(node)])
    command = ['systemd-run', '--unit', unit_name(node), '--property=Type=exec', '--property=LimitMEMLOCK=infinity',
               '--property=LimitNOFILE=65536', '--property=UMask=0077', '--working-directory', remote_path(node),
               *node_command(node, action, resume)]
    return dict(status='launched', unit=unit_name(node), output=ssh.call(command))


def operate(node, action, out, snapshot=None, resume=False, transport=SSH):
    ssh = transport(node)
    ssh.preflight()
    if action == 'status':
        return json.loads(ssh.call(['python3', '-c', STATUS, remote_path(node), unit_name(node)]))
    if action == 'collect':
        payload = ssh.call(['python3', '-c', COLLECT, remote_path(node)], binary=True, timeout=1800)
        path = out / f"{node['name']}-artifacts-{time.time_ns()}.tar.gz"
        with path.open('xb') as stream: stream.write(payload)
        checksum = sha(payload); path.with_suffix(path.suffix + '.sha256').write_text(f'{checksum}  {path.name}\n')
        return dict(status='collected', path=str(path), sha256=checksum)
    payload, checksum, manifest = bundle(snapshot, node)
    staged = json.loads(ssh.call(['python3', '-c', STAGE, remote_path(node, action), checksum, str(int(resume))], data=payload))
    if action in ('run', 'cleanup'):
        result = launch(ssh, node, action, resume)
    else:
        result = dict(status='completed', output=ssh.call(node_command(node, action, resume), timeout=600))
        if action == 'inventory':
            text = ssh.call(['cat', f'{remote_path(node, action)}/artifacts/inventory/inventory.json'])
            path = out / f"{node['name']}-inventory.json"; path.write_text(text)
            result['inventory_file'] = str(path)
    result['bundle'] = staged
    return result


def parallel(nodes, workers, operation):
    def one(node):
        try: return dict(name=node['name'], ok=True, result=operation(node))
        except Exception as error: return dict(name=node['name'], ok=False, error=f'{type(error).__name__}: {error}')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, nodes))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['plan', 'inventory', 'run', 'status', 'collect', 'cleanup'])
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--out', type=Path, help='local evidence directory; defaults to ~/.cache/mooncake-qualification/RUN_ID')
    p.add_argument('--parallel', type=int, default=4)
    p.add_argument('--apply', action='store_true'); p.add_argument('--erase-data', action='store_true')
    p.add_argument('--resume', action='store_true'); p.add_argument('--remote', action='store_true', help='execute a read-only plan on each node')
    args = p.parse_args()
    if args.parallel < 1 or args.parallel > 32: p.error('--parallel must be between 1 and 32')
    if args.command == 'run' and not (args.apply and args.erase_data): p.error('run requires --apply --erase-data')
    if args.command != 'run' and args.erase_data: p.error('--erase-data applies only to run')
    if args.command == 'cleanup' and not (args.apply and args.resume): p.error('cleanup requires --apply --resume')
    fleet = load_config(args.config, complete=args.command in ('run', 'cleanup') or (args.command == 'plan' and args.remote))
    args.out = args.out or Path.home() / '.cache/mooncake-qualification' / fleet['run_id']
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == 'plan' and not args.remote:
        dirty = bool(subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=all'],capture_output=True,text=True,check=True).stdout)
        result = dict(schema=1, run_id=fleet['run_id'], local_preview=True, dirty_source=dirty,
                      nodes=[dict(name=n['name'], ssh=n['ssh'], remote_path=remote_path(n, 'plan'), run_path=remote_path(n), unit=unit_name(n),
                                  command=node_command(n, 'plan'), erase_requires='run --apply --erase-data') for n in fleet['nodes']])
    else:
        snapshot = source_snapshot() if args.command not in ('status', 'collect') else None
        result = dict(schema=1, run_id=fleet['run_id'], results=parallel(fleet['nodes'], args.parallel,
                      lambda node: operate(node, args.command, args.out, snapshot, args.resume)))
    target = args.out / f'{args.command}-{time.time_ns()}.json'; target.write_bytes(json_bytes(result))
    print(json.dumps(result, indent=2))
    if 'results' in result and not all(row['ok'] for row in result['results']): raise SystemExit(1)


if __name__ == '__main__': main()
