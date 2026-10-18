import hashlib
import io
import json
import sqlite3
from pathlib import Path

from botocore.stub import Stubber, ANY
import pytest

from facts import handoff
from facts.artifact_store import DirectoryStore, S3Store, digest_file, ensure_object, safe_path
from facts.core import Store, dumps


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path/'corpus'; s = Store(root)
    s.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    s.db.commit(); s.db.close()
    (root/'storage'/'packet.txt').write_text('saved source packet')
    (root/'storage'/'.env.private').write_text('exclude me')
    (root/'storage'/'backups').mkdir()
    (root/'storage'/'backups'/'old.sqlite').write_text('old backup')
    return root


def test_backup_restore_and_remote_resume(corpus, tmp_path):
    bundle=tmp_path/'bundle'; result=handoff.backup(corpus,bundle); sid=result['snapshot']
    manifest=handoff.load_manifest(bundle,sid)
    assert {f['path'] for f in manifest['files']} == {'storage/catalog.sqlite','storage/packet.txt'}
    destination=tmp_path/'restored'
    assert handoff.restore(bundle,sid,destination)['integrity']=='ok'
    assert (destination/'storage/packet.txt').read_text()=='saved source packet'
    with pytest.raises(ValueError): handoff.restore(bundle,sid,destination)
    remote=DirectoryStore(tmp_path/'remote')
    first=handoff.deliver(bundle,sid,remote)
    assert first['uploaded_objects']==2
    assert handoff.deliver(bundle,sid,remote)['uploaded_objects']==0
    received=tmp_path/'received'
    assert handoff.receive(received,sid,remote)['integrity']=='ok'
    handoff.restore(received,sid,tmp_path/'remote-restored')


def test_missing_corrupt_and_interrupted_delivery_never_publish_success(corpus,tmp_path,monkeypatch):
    bundle=tmp_path/'bundle'; sid=handoff.backup(corpus,bundle)['snapshot']
    remote=DirectoryStore(tmp_path/'remote'); original=remote.put_file; calls=[]
    def fail(key,source):
        calls.append(key)
        if len(calls)==2: raise OSError('simulated outage')
        return original(key,source)
    monkeypatch.setattr(remote,'put_file',fail)
    with pytest.raises(OSError): handoff.deliver(bundle,sid,remote,1)
    assert not remote.path(handoff.manifest_key(sid)).exists()
    assert not (bundle/'receipts').exists()
    monkeypatch.setattr(remote,'put_file',original)
    assert handoff.deliver(bundle,sid,remote,1)['reused_objects']==1
    receipt=next((bundle/'receipts').rglob('*.json')).read_bytes()
    member=handoff.load_manifest(bundle,sid)['files'][0]
    remote.path(handoff.object_key(member['sha256'])).write_bytes(b'corrupt')
    with pytest.raises(ValueError): handoff.deliver(bundle,sid,remote)
    assert next((bundle/'receipts').rglob('*.json')).read_bytes()==receipt
    with pytest.raises(ValueError): handoff.receive(tmp_path/'received',sid,remote)
    assert not (tmp_path/'received/latest.json').exists()
    local=DirectoryStore(bundle).path(handoff.object_key(member['sha256']))
    local.unlink()
    with pytest.raises(FileNotFoundError): handoff.verify(bundle,sid)


@pytest.mark.parametrize('path',['../x','/x','a//x','./x','a/../x','a\\x',''])
def test_unsafe_paths(path):
    with pytest.raises(ValueError): safe_path(path)


def test_backup_failure_keeps_previous_manifest(corpus,tmp_path,monkeypatch):
    bundle=tmp_path/'bundle'; handoff.backup(corpus,bundle)
    previous=(bundle/'latest.json').read_bytes()
    def fail(*a): raise OSError('disk full')
    monkeypatch.setattr(handoff,'ensure_object',fail)
    with pytest.raises(OSError): handoff.backup(corpus,bundle)
    assert (bundle/'latest.json').read_bytes()==previous


def test_backup_rejects_missing_retained_evidence_and_unsafe_bundle_locations(corpus,tmp_path):
    for bundle in [corpus,corpus/'storage',corpus/'storage/objects',corpus.parent]:
        with pytest.raises(ValueError): handoff.backup(corpus,bundle)
    store=Store(corpus);sid=store.source('topic','https://example.com/report')
    cid=store.capture(sid,'direct_http',b'missing evidence','text')
    path=store.db.execute('SELECT artifact FROM captures WHERE id=?',(cid,)).fetchone()[0]
    store.db.commit();store.db.close()
    bundle=tmp_path/'bundle';sid=handoff.backup(corpus,bundle)['snapshot']
    payload=handoff.load_manifest(bundle,sid)
    payload['files']=[f for f in payload['files'] if f['path']!=path]
    content=dumps(payload).encode();changed=hashlib.sha256(content).hexdigest()
    DirectoryStore(bundle).path(handoff.manifest_key(changed)).write_bytes(content)
    with pytest.raises(ValueError,match='omits catalog-referenced'):
        handoff.verify(bundle,changed)
    previous=(bundle/'latest.json').read_bytes()
    (corpus/path).unlink()
    with pytest.raises(ValueError,match='missing catalog-referenced'):
        handoff.backup(corpus,bundle)
    assert (bundle/'latest.json').read_bytes()==previous


def test_manifest_rejects_sqlite_sidecars_and_traversal(corpus,tmp_path):
    bundle=tmp_path/'bundle'; sid=handoff.backup(corpus,bundle)['snapshot']
    payload=handoff.load_manifest(bundle,sid)
    for bad in ['../escape','storage/catalog.sqlite-wal']:
        changed=json.loads(dumps(payload)); changed['files'][1]['path']=bad
        content=dumps(changed).encode(); identity=hashlib.sha256(content).hexdigest()
        DirectoryStore(bundle).path(handoff.manifest_key(identity)).write_bytes(content)
        with pytest.raises(ValueError): handoff.load_manifest(bundle,identity)


def test_s3_uses_conditional_writes_and_reads_bytes(tmp_path):
    store=S3Store({'FACTS_S3_ENDPOINT':'https://s3.example.com','FACTS_S3_BUCKET':'evidence',
        'FACTS_S3_ACCESS_KEY_ID':'fixture-key','FACTS_S3_SECRET_ACCESS_KEY':'fixture-secret'})
    source=tmp_path/'source';source.write_bytes(b'evidence');digest,size=digest_file(source)
    expected={'Bucket':'evidence','Key':'facts-v1/object'}
    with Stubber(store.client) as stub:
        stub.add_client_error('get_object','NoSuchKey',http_status_code=404,expected_params=expected)
        stub.add_response('put_object',{}, {**expected,'Body':ANY,'IfNoneMatch':'*'})
        stub.add_response('get_object',{'Body':io.BytesIO(b'evidence')},expected)
        assert ensure_object(store,'object',source,digest,size)
        stub.assert_no_pending_responses()


def test_store_rejects_symlinks_and_preserves_existing_bytes(tmp_path):
    store=DirectoryStore(tmp_path/'store');outside=tmp_path/'outside';outside.mkdir()
    (store.root/'escape').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError): store.path('escape/evidence')
    source=tmp_path/'source';source.write_bytes(b'one');digest,size=digest_file(source)
    ensure_object(store,'evidence',source,digest,size)
    source.write_bytes(b'two')
    assert store.put_file('evidence',source) is False
    assert store.path('evidence').read_bytes()==b'one'


def test_audit_checks_retained_artifact_references(corpus):
    from facts.audit import audit
    from facts.organize import organize
    store=Store(corpus)
    sid=store.source('topic','https://example.com/report')
    cid=store.capture(sid,'direct_http',b'saved evidence','retained text')
    store.db.commit()
    path=corpus/store.db.execute('SELECT artifact FROM captures WHERE id=?',(cid,)).fetchone()[0]
    store.db.close()
    organize(corpus,cached_only=True)
    assert audit(corpus,current=True)['artifact_references_checked']==2
    path.unlink()
    with pytest.raises(ValueError,match='Missing or unsafe artifact'):
        audit(corpus,current=True)


@pytest.mark.parametrize('reference', ['search', 'extraction_input', 'extraction_output', 'attempt', 'followup'])
def test_every_catalog_artifact_is_required_by_audit_backup_and_verify(corpus, tmp_path, reference):
    from facts.audit import audit
    from facts.core import import_packet
    from facts.organize import organize
    store = Store(corpus)
    artifact = store.put({'retained': reference})
    if reference == 'search':
        store.db.execute("INSERT INTO searches(id,topic,family,payload,artifact) VALUES ('search','topic','fixture','{}',?)", (artifact,))
    elif reference.startswith('extraction_'):
        source = store.source('topic', 'https://example.com/report')
        capture = store.capture(source, 'direct_http', b'capture', 'source text')
        text = artifact if reference == 'extraction_input' else store.put('input')
        output = artifact if reference == 'extraction_output' else None
        store.db.execute("INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,artifact) VALUES ('job','topic',?,0,?,'fixture',?)", (capture, text, output))
    elif reference == 'attempt':
        store.db.execute("INSERT INTO attempts(id,stage,target,started,outcome,artifact,metadata) VALUES ('attempt','search','search','2026-01-01','ok',?,'{}')", (artifact,))
    else:
        packet = tmp_path/'packet.json'
        packet.write_text(dumps({'followup_queries': ['Find retained evidence']}))
        import_packet(store, 'topic', packet)
        artifact = store.db.execute('SELECT origin FROM followups').fetchone()[0]
        store.db.execute("INSERT INTO followups VALUES ('topic','Job-origin query','extraction-job-id')")
    store.db.commit(); store.db.close()
    organize(corpus, cached_only=True)
    with sqlite3.connect(corpus/'storage/catalog.sqlite') as catalog:
        assert artifact in handoff.catalog_artifacts(catalog)
    catalog.close()
    assert audit(corpus, current=True)['artifact_references_checked'] >= 1
    bundle = tmp_path/'bundle'; snapshot = handoff.backup(corpus, bundle)['snapshot']
    payload = handoff.load_manifest(bundle, snapshot)
    payload['files'] = [member for member in payload['files'] if member['path'] != artifact]
    content = dumps(payload).encode(); identity = hashlib.sha256(content).hexdigest()
    DirectoryStore(bundle).path(handoff.manifest_key(identity)).write_bytes(content)
    with pytest.raises(ValueError, match='omits catalog-referenced'):
        handoff.verify(bundle, identity)
    (corpus/artifact).unlink()
    with pytest.raises(ValueError, match='Missing or unsafe artifact'):
        audit(corpus, current=True)
    with pytest.raises(ValueError, match='missing catalog-referenced'):
        handoff.backup(corpus, bundle)
