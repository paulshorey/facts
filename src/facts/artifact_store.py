"""Private immutable-key delivery. Credentials never enter manifests or receipts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import urlsplit


class MissingObject(FileNotFoundError):
    pass


def safe_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')) or '\\' in value:
        raise ValueError('Unsafe artifact path')
    return path


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


class DirectoryStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.identity = 'directory:' + str(self.root)

    def path(self, key):
        path = self.root / str(safe_path(key))
        if not path.resolve().is_relative_to(self.root) or any(p.is_symlink() for p in [path, *path.parents] if p != self.root):
            raise ValueError('Artifact path escapes its store')
        return path

    def open(self, key):
        try:
            return self.path(key).open('rb')
        except FileNotFoundError:
            raise MissingObject(key) from None

    def put_file(self, key, source):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                with Path(source).open('rb') as stream:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
                output.flush(); os.fsync(output.fileno())
                try:
                    os.link(temporary, target)
                    return True
                except FileExistsError:
                    return False
            finally:
                temporary.unlink(missing_ok=True)


class S3Store:
    def __init__(self, environ=None):
        import boto3
        from botocore.config import Config
        env = os.environ if environ is None else environ
        names = ['FACTS_S3_ENDPOINT', 'FACTS_S3_BUCKET', 'FACTS_S3_ACCESS_KEY_ID', 'FACTS_S3_SECRET_ACCESS_KEY']
        if any(not env.get(k) for k in names):
            raise ValueError('Configure the four FACTS_S3 connection variables')
        endpoint = urlsplit(env['FACTS_S3_ENDPOINT'])
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('Artifact endpoint must be credential-free HTTPS')
        self.bucket = env['FACTS_S3_BUCKET']
        self.prefix = env.get('FACTS_S3_PREFIX', 'facts-v1').strip('/')
        safe_path(self.prefix)
        self.identity = f's3:{endpoint.hostname}/{self.bucket}/{self.prefix}'
        self.client = boto3.client('s3', endpoint_url=env['FACTS_S3_ENDPOINT'],
            aws_access_key_id=env['FACTS_S3_ACCESS_KEY_ID'], aws_secret_access_key=env['FACTS_S3_SECRET_ACCESS_KEY'],
            region_name=env.get('FACTS_S3_REGION', 'auto'),
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                connect_timeout=20, read_timeout=180, max_pool_connections=32, retries={'max_attempts': 3},
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))

    def key(self, key):
        return self.prefix + '/' + str(safe_path(key))

    def open(self, key):
        from botocore.exceptions import ClientError
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self.key(key))['Body']
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') in {'NoSuchKey', '404', 'NotFound'}:
                raise MissingObject(key) from None
            raise RuntimeError('Artifact download failed') from None

    def put_file(self, key, source):
        from botocore.exceptions import ClientError
        try:
            with Path(source).open('rb') as body:
                self.client.put_object(Bucket=self.bucket, Key=self.key(key), Body=body, IfNoneMatch='*')
            return True
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') in {'PreconditionFailed', '412'}:
                return False
            raise RuntimeError('Artifact upload failed') from None


def verify_object(store, key, digest, size):
    calculated = hashlib.sha256(); length = 0
    with store.open(key) as stream:
        while chunk := stream.read(1024 * 1024):
            calculated.update(chunk); length += len(chunk)
    if length != size or calculated.hexdigest() != digest:
        raise ValueError('Artifact bytes do not match their manifest: ' + key)


def ensure_object(store, key, source, digest, size):
    try:
        verify_object(store, key, digest, size)
        return False
    except MissingObject:
        uploaded = store.put_file(key, source)
        verify_object(store, key, digest, size)
        return uploaded
