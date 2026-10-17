"""Verified portable snapshots, immutable delivery and isolated restoration."""
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

from .artifact_store import DirectoryStore, S3Store, digest_file, ensure_object, safe_path, verify_object
from .core import ROOT, dumps, now

VERSION = 'handoff.v1'
PRESERVED_TABLES = ('topics', 'searches', 'sources', 'topic_sources', 'captures', 'items', 'datasets', 'extraction_jobs', 'followups', 'attempts')


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


def catalog_fingerprint(database, tables=PRESERVED_TABLES):
    result = {}
    for table in tables:
        if table not in PRESERVED_TABLES:
            raise ValueError('Unknown original catalog table')
        digest = hashlib.sha256(); count = 0
        for row in database.execute(f'SELECT * FROM {table} ORDER BY rowid'):
            digest.update(dumps(list(row)).encode()); digest.update(b'\n'); count += 1
        result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def load_manifest(bundle, snapshot):
    path = DirectoryStore(bundle).path(manifest_key(snapshot))
    payload = json.loads(path.read_text())
    if payload.get('schema_version') != VERSION or hashlib.sha256(dumps(payload).encode()).hexdigest() != snapshot:
        raise ValueError('Invalid handoff manifest identity')
    # Early v1 snapshots did not include the append-only attempts fingerprint.
    # Their catalog bytes are still verified; never invalidate an existing backup
    # merely because the fingerprint inventory gains a table.
    if not set(PRESERVED_TABLES)-{'attempts'} <= set(payload['original_tables']) <= set(PRESERVED_TABLES):
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


def backup(root=ROOT, bundle=None):
    root = Path(root).resolve(); bundle = Path(bundle or root / 'storage/handoffs').resolve()
    store = DirectoryStore(bundle)
    with tempfile.TemporaryDirectory(prefix='.backup-', dir=bundle) as scratch:
        snapshot = Path(scratch) / 'catalog.sqlite'
        with sqlite3.connect(f'file:{root / "storage/catalog.sqlite"}?mode=ro', uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Catalog integrity check failed')
            original = catalog_fingerprint(target)
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
        payload = {'schema_version': VERSION, 'created_at': now(), 'files': files, 'original_tables': original}
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
    parser.add_argument('command', choices=['backup', 'verify', 'restore', 'deliver', 'receive'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--snapshot')
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--remote-directory', type=Path, help='Use a filesystem mirror instead of configured S3')
    parser.add_argument('--workers', type=int, default=8)
    args = parser.parse_args(); bundle = args.bundle or args.root / 'storage/handoffs'
    try:
        if args.command == 'backup':
            result = backup(args.root, bundle)
        else:
            if not args.snapshot:
                parser.error('--snapshot is required')
            if args.command == 'verify': result = verify(bundle, args.snapshot)
            elif args.command == 'restore':
                if args.destination is None: parser.error('--destination is required')
                result = restore(bundle, args.snapshot, args.destination)
            else:
                remote = DirectoryStore(args.remote_directory) if args.remote_directory else S3Store()
                def progress(done, total):
                    if done % 100 == 0 or done == total:
                        print(f'{args.command}: verified {done}/{total} objects', file=sys.stderr, flush=True)
                result = (deliver(bundle, args.snapshot, remote, args.workers, progress) if args.command == 'deliver'
                          else receive(bundle, args.snapshot, remote, args.workers, progress))
        print(json.dumps(result, indent=2))
    except Exception as error:
        raise SystemExit(f'Handoff failed: {type(error).__name__}. Inspect inputs/configuration without printing credentials.') from None


if __name__ == '__main__': main()
