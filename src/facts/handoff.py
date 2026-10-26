"""Verified portable snapshots, replica delivery and isolated restoration."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from uuid import uuid4

from .artifact_store import DirectoryStore, S3Store, digest_file, ensure_object, safe_path, verify_object
from .core import ROOT, dumps, now
from .runtime import exclusive_writer

VERSION = 'handoff.v2'
PRESERVED_TABLES = ('topics', 'searches', 'sources', 'topic_sources', 'captures', 'items', 'datasets', 'extraction_jobs', 'followups', 'attempts')
# Frozen contracts for published v1 snapshots, independent of future writer tables.
V1_REQUIRED = frozenset(('topics', 'searches', 'sources', 'topic_sources', 'captures', 'items', 'datasets', 'extraction_jobs', 'followups'))
V1_ALLOWED = V1_REQUIRED | {'attempts'}


def object_key(digest):
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Invalid content digest')
    return f'objects/{digest[:2]}/{digest}'


def manifest_key(snapshot):
    object_key(snapshot)
    return f'manifests/{snapshot}.json'


def journal(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(dumps(value)+'\n'); stream.flush(); os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def catalog_fingerprint(database, tables=None):
    result = {}
    if tables is None: tables = PRESERVED_TABLES
    for table in tables:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', table):
            raise ValueError('Invalid original catalog table')
        digest = hashlib.sha256(); count = 0
        for row in database.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            digest.update(dumps(list(row)).encode()); digest.update(b'\n'); count += 1
        result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def catalog_artifacts(database):
    """Paths promised by retained catalog rows, including historical versions."""
    paths=set()
    def collect(value,key=''):
        if isinstance(value,dict):
            for name,child in value.items(): collect(child,name)
        elif isinstance(value,list):
            for child in value: collect(child,key)
        elif (isinstance(value,str) and value.startswith(('storage/','exports/'))
                and (key.endswith(('_artifact','_artifacts')) or key in {'artifact','parquet'})):
            paths.add(value)
    tables=[r[0] for r in database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        # Quote SQLite catalog identifiers, including FTS's own shadow tables.
        quoted='"'+table.replace('"','""')+'"'
        columns=[r[1] for r in database.execute(f'PRAGMA table_info({quoted})')]
        selected=[c for c in columns if c in {'payload','metadata','artifact','parquet'} or c.endswith('_artifact')
                  or (table == 'followups' and c == 'origin')]
        if not selected: continue
        for row in database.execute('SELECT '+','.join('"'+c.replace('"','""')+'"' for c in selected)+' FROM '+quoted):
            for key,value in zip(selected,row):
                if value is None: continue
                collect(json.loads(value) if key in {'payload','metadata'} else value, 'artifact' if table == 'followups' and key == 'origin' else key)
    return paths


def load_manifest(bundle, snapshot):
    path = DirectoryStore(bundle).path(manifest_key(snapshot))
    raw = path.read_bytes(); payload = json.loads(raw)
    version = payload.get('schema_version')
    if version not in {'handoff.v1', VERSION} or hashlib.sha256(raw).hexdigest() != snapshot:
        raise ValueError('Invalid handoff manifest identity')
    tables = set(payload['original_tables'])
    if version == 'handoff.v1':
        valid_inventory = V1_REQUIRED <= tables <= V1_ALLOWED
    else:
        inventory = payload.get('original_table_inventory', [])
        valid_inventory = (isinstance(inventory, list) and len(inventory) == len(set(inventory))
                           and tables == set(inventory) and V1_REQUIRED <= tables)
    if not valid_inventory or any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name) for name in tables):
        raise ValueError('Invalid original catalog inventory')
    seen = set(); sizes = {}
    for item in payload['files']:
        path = str(safe_path(item['path']))
        if (path.split('/')[0] not in {'storage', 'exports', 'topics'} or path in seen
                or path.endswith(('-wal', '-shm')) or any(p.startswith('.') for p in path.split('/'))):
            raise ValueError('Invalid or duplicate handoff member')
        object_key(item['sha256'])
        if type(item['size']) is not int or item['size'] < 0:
            raise ValueError('Invalid handoff member size')
        if item['sha256'] in sizes and sizes[item['sha256']] != item['size']:
            raise ValueError('Conflicting sizes for one content digest')
        sizes[item['sha256']] = item['size']
        seen.add(path)
    for path in seen:
        if any(str(parent) in seen for parent in safe_path(path).parents if str(parent) != '.'):
            raise ValueError('Handoff file is also a directory')
    if 'storage/catalog.sqlite' not in seen:
        raise ValueError('Handoff has no catalog')
    return payload


@exclusive_writer
def backup(root=ROOT, bundle=None):
    root = Path(root).resolve(); bundle = Path(bundle or root / 'storage/handoffs').resolve()
    if root.is_relative_to(bundle) or (bundle.is_relative_to(root) and not bundle.is_relative_to(root/'storage/handoffs')):
        raise ValueError('Use storage/handoffs inside the collector or a separate external bundle')
    store = DirectoryStore(bundle)
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=bundle) as scratch:
        snapshot = Path(scratch) / 'catalog.sqlite'
        with sqlite3.connect(f'file:{root / "storage/catalog.sqlite"}?mode=ro', uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Catalog integrity check failed')
            original = catalog_fingerprint(target)
            required_artifacts = catalog_artifacts(target)
        source.close(); target.close()
        entries = [('storage/catalog.sqlite', snapshot)]
        for folder in ['storage', 'exports', 'topics']:
            for path in sorted((root / folder).rglob('*')):
                relative = path.relative_to(root)
                if path.is_relative_to(bundle) or any(p in {'backups', 'locks', 'handoffs', '__pycache__'} for p in relative.parts):
                    continue
                if path.is_symlink():
                    raise ValueError('Symlinks are not portable evidence')
                if not path.is_file() or '.sqlite' in path.name or any(p.startswith('.') for p in relative.parts) or path.suffix in {'.tmp', '.lock', '.env', '.pem', '.key'}:
                    continue
                if path.name.startswith('.env'):
                    continue
                entries.append((relative.as_posix(), path))
        files = []
        for relative, path in entries:
            before = path.stat()
            digest, size = digest_file(path)
            if relative.startswith('storage/objects/') and path.name != digest:
                raise ValueError('Original content-addressed object is corrupt: ' + relative)
            ensure_object(store, object_key(digest), path, digest, size)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('Source file changed during backup: ' + relative)
            files.append({'path': relative, 'sha256': digest, 'size': size})
        if required_artifacts - {item['path'] for item in files}:
            raise ValueError('Backup is missing catalog-referenced artifacts')
        payload = {'schema_version': VERSION, 'created_at': now(), 'files': files,
                   'original_tables': original, 'original_table_inventory': list(original)}
        identity = hashlib.sha256(dumps(payload).encode()).hexdigest()
        candidate = Path(scratch) / 'manifest.json'; candidate.write_text(dumps(payload))
        digest, size = digest_file(candidate)
        ensure_object(store, manifest_key(identity), candidate, digest, size)
        result = {'snapshot': identity, 'files': len(files), 'logical_bytes': sum(f['size'] for f in files)}
        journal(bundle / 'latest.json', result)
        return result


def verify(bundle, snapshot):
    payload = load_manifest(bundle, snapshot); store = DirectoryStore(bundle)
    for item in {f['sha256']: f for f in payload['files']}.values():
        verify_object(store, object_key(item['sha256']), item['sha256'], item['size'])
    catalog = next(f for f in payload['files'] if f['path'] == 'storage/catalog.sqlite')
    with sqlite3.connect(f'file:{store.path(object_key(catalog["sha256"]))}?mode=ro&immutable=1', uri=True) as database:
        if (database.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                or catalog_fingerprint(database, payload['original_tables']) != payload['original_tables']):
            raise ValueError('Restored catalog does not match its manifest')
        if catalog_artifacts(database) - {item['path'] for item in payload['files']}:
            raise ValueError('Handoff omits catalog-referenced artifacts')
    database.close()
    return {'snapshot': snapshot, 'verified_files': len(payload['files']), 'integrity': 'ok'}


def restore(bundle, snapshot, destination):
    payload = load_manifest(bundle, snapshot); verify(bundle, snapshot)
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Restore requires a new destination directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.restore-') as temporary:
        scratch = Path(temporary) / 'restored'; scratch.mkdir(mode=0o700)
        store = DirectoryStore(bundle)
        for item in payload['files']:
            path = scratch / str(safe_path(item['path'])); path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(store.path(object_key(item['sha256'])), path); path.chmod(0o600)
            if digest_file(path) != (item['sha256'], item['size']):
                raise ValueError('Restored file does not match manifest')
        scratch.rename(destination)
    return {'snapshot': snapshot, 'restored_files': len(payload['files']), 'integrity': 'ok'}


@exclusive_writer
def deliver(bundle, snapshot, remote, workers=4, progress=None):
    if workers not in range(1, 33):
        raise ValueError('Use one through thirty-two delivery workers')
    payload = load_manifest(bundle, snapshot); verify(bundle, snapshot)
    local = DirectoryStore(bundle)
    objects = list({f['sha256']: f for f in payload['files']}.values())
    def send(item):
        key = object_key(item['sha256'])
        return ensure_object(remote, key, local.path(key), item['sha256'], item['size'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        uploaded = 0
        futures = [pool.submit(send, item) for item in objects]
        for index, future in enumerate(as_completed(futures), 1):
            uploaded += future.result()
            if progress: progress(index, len(objects))
    path = local.path(manifest_key(snapshot)); digest, size = digest_file(path)
    ensure_object(remote, manifest_key(snapshot), path, digest, size)
    receipt = {'snapshot': snapshot, 'destination': remote.identity, 'verified_objects': len(objects),
               'uploaded_objects': uploaded, 'reused_objects': len(objects)-uploaded, 'verified_at': now()}
    identity = hashlib.sha256(remote.identity.encode()).hexdigest()[:16]
    journal(Path(bundle) / 'receipts' / snapshot / (identity+'.json'), receipt)
    return receipt


def list_snapshots(store):
    """Discovery only: listings do not prove completeness or object integrity."""
    entries = []
    for key in store.keys('manifests'):
        match = re.fullmatch(r'manifests/([0-9a-f]{64})\.json', key)
        if not match: continue
        with store.open(key) as stream: raw = stream.read()
        value = json.loads(raw)
        if hashlib.sha256(raw).hexdigest() != match[1]:
            raise ValueError('Listed manifest identity mismatch')
        entries.append({'snapshot': match[1], 'created_at': value.get('created_at'),
                        'files': len(value['files']), 'schema_version': value.get('schema_version')})
    return {'destination': store.identity, 'snapshots': entries,
            'listing_is_authoritative': False, 'object_bytes_verified': False}


@exclusive_writer
def repair(bundle, snapshot, remote, digest=None):
    """Replace only a damaged replica key from a verified bundle, preserving damage."""
    payload = load_manifest(bundle, snapshot); verify(bundle, snapshot)
    local = DirectoryStore(bundle)
    if digest is None:
        key = manifest_key(snapshot)
    else:
        if digest not in {f['sha256'] for f in payload['files']}:
            raise ValueError('Repair digest is outside this snapshot')
        key = object_key(digest)
    source = local.path(key); expected, size = digest_file(source)
    try:
        verify_object(remote, key, expected, size)
        return {'snapshot': snapshot, 'key': key, 'state': 'already_valid'}
    except FileNotFoundError:
        ensure_object(remote, key, source, expected, size)
        result = {'snapshot': snapshot, 'key': key, 'state': 'restored_missing', 'destination': remote.identity}
    except ValueError:
        folder = Path(bundle)/'quarantine'/uuid4().hex; folder.mkdir(parents=True, mode=0o700)
        damaged = folder/'damaged-object'
        with remote.open(key) as original, damaged.open('xb') as stream:
            damaged.chmod(0o600); shutil.copyfileobj(original, stream)
            stream.flush(); os.fsync(stream.fileno())
        observed = digest_file(damaged)
        result = {'snapshot': snapshot, 'key': key, 'destination': remote.identity,
                  'observed_sha256': observed[0], 'observed_size': observed[1],
                  'expected_sha256': expected, 'quarantined_at': now(), 'state': 'quarantined'}
        journal(folder/'repair.json', result)
        # Detect an intervening change; this is a single-host safeguard, not CAS.
        with tempfile.NamedTemporaryFile(dir=folder) as check:
            with remote.open(key) as original: shutil.copyfileobj(original, check)
            check.flush()
            if digest_file(check.name) != observed:
                raise ValueError('Replica changed during repair; inspect quarantine before retrying')
        remote.replace_file(key, source)
        verify_object(remote, key, expected, size)
        result['state'] = 'repaired'; journal(folder/'repair.json', result)
    result['verified_at'] = now()
    journal(Path(bundle)/'repairs'/snapshot/(uuid4().hex+'.json'), result)
    return result


def receive(bundle, snapshot, remote, workers=4, progress=None):
    if workers not in range(1, 33):
        raise ValueError('Use one through thirty-two retrieval workers')
    local = DirectoryStore(bundle)
    # Manifest bytes are addressed by their own digest, just like file objects.
    key = manifest_key(snapshot)
    with tempfile.TemporaryDirectory(dir=local.root) as temp:
        manifest = Path(temp) / 'manifest'
        with remote.open(key) as stream, manifest.open('wb') as output:
            shutil.copyfileobj(stream, output)
        digest, size = digest_file(manifest)
        if digest != snapshot:
            raise ValueError('Remote manifest digest mismatch')
        ensure_object(local, key, manifest, digest, size)
        payload = load_manifest(bundle, snapshot)
        def retrieve(item):
            key = object_key(item['sha256'])
            try:
                verify_object(local, key, item['sha256'], item['size']); return
            except FileNotFoundError:
                pass
            with tempfile.NamedTemporaryFile(dir=temp) as output:
                path = Path(output.name)
                with remote.open(key) as stream:
                    shutil.copyfileobj(stream, output)
                output.flush()
                if digest_file(path) != (item['sha256'], item['size']):
                    raise ValueError('Remote object digest mismatch')
                ensure_object(local, key, path, item['sha256'], item['size'])
        objects = list({f['sha256']: f for f in payload['files']}.values())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(retrieve, item) for item in objects]
            for index, future in enumerate(as_completed(futures), 1):
                future.result()
                if progress: progress(index, len(objects))
    result = verify(bundle, snapshot)
    journal(Path(bundle) / 'latest.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['backup', 'verify', 'restore', 'deliver', 'receive', 'list', 'repair'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--snapshot')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--remote-directory', type=Path, help='Use a filesystem mirror instead of configured S3')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--remote', action='store_true', help='List configured S3 rather than the local bundle')
    parser.add_argument('--object-digest', help='Repair this file digest; omit to repair the snapshot manifest')
    parser.add_argument('--confirm-repair', action='store_true', help='Explicitly replace only a quarantined damaged replica key')
    args = parser.parse_args(); bundle = args.bundle or args.root / 'storage/handoffs'
    if args.remote and args.command != 'list':
        parser.error('--remote applies to list; use receive for independent remote verification')
    if (args.object_digest or args.confirm_repair) and args.command != 'repair':
        parser.error('Repair options require repair')
    if args.remote_directory and args.command not in {'list','deliver','receive','repair'}:
        parser.error('--remote-directory requires a replica operation')
    try:
        if args.command == 'backup':
            result = backup(args.root, bundle)
        elif args.command == 'list':
            selected = (DirectoryStore(args.remote_directory) if args.remote_directory else S3Store()) if args.remote or args.remote_directory else DirectoryStore(bundle)
            result = list_snapshots(selected)
        else:
            if not args.snapshot:
                parser.error('--snapshot is required')
            if args.command == 'verify': result = verify(bundle, args.snapshot)
            elif args.command == 'restore':
                if args.destination is None: parser.error('--destination is required')
                result = restore(bundle, args.snapshot, args.destination)
            else:
                remote = DirectoryStore(args.remote_directory) if args.remote_directory else S3Store()
                if args.command == 'repair':
                    if not args.confirm_repair: parser.error('repair requires --confirm-repair')
                    print(json.dumps(repair(bundle, args.snapshot, remote, args.object_digest), indent=2)); return
                def progress(done, total):
                    if done % 100 == 0 or done == total:
                        print(f'{args.command}: verified {done}/{total} objects', file=sys.stderr, flush=True)
                result = (deliver(bundle, args.snapshot, remote, args.workers, progress) if args.command == 'deliver'
                          else receive(bundle, args.snapshot, remote, args.workers, progress))
        print(json.dumps(result, indent=2))
    except Exception as error:
        raise SystemExit(f'Handoff failed: {type(error).__name__}. Inspect inputs/configuration without printing credentials.') from None


if __name__ == '__main__': main()
