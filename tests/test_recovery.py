import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from botocore.stub import Stubber

from facts import handoff, postgres_sync
from facts.artifact_store import DirectoryStore, S3Store
from facts.core import Store, dumps
from facts.runtime import writer_lock


@pytest.fixture
def saved(tmp_path):
    root=tmp_path/'source';store=Store(root)
    store.db.execute("INSERT INTO topics VALUES ('topic','{}')");store.db.commit();store.db.close()
    bundle=tmp_path/'bundle';snapshot=handoff.backup(root,bundle)['snapshot']
    return root,bundle,snapshot


def publish(bundle,payload):
    content=dumps(payload).encode();snapshot=hashlib.sha256(content).hexdigest()
    DirectoryStore(bundle).path(handoff.manifest_key(snapshot)).write_bytes(content)
    return snapshot


def test_existing_manifests_survive_future_writer_inventory(saved,monkeypatch):
    root,bundle,snapshot=saved
    v1=handoff.load_manifest(bundle,snapshot);v1['schema_version']='handoff.v1';v1.pop('original_table_inventory')
    v1.pop('unused',None);v1['original_tables'].pop('attempts')
    legacy=publish(bundle,v1)
    monkeypatch.setattr(handoff,'PRESERVED_TABLES',(*handoff.PRESERVED_TABLES,'future_original'))
    assert handoff.verify(bundle,legacy)['integrity']=='ok'
    assert handoff.verify(bundle,snapshot)['integrity']=='ok'


def test_listing_recovers_snapshot_without_latest(saved):
    _,bundle,snapshot=saved;(bundle/'latest.json').unlink()
    report=handoff.list_snapshots(DirectoryStore(bundle))
    assert report['snapshots'][0]['snapshot']==snapshot
    assert not report['object_bytes_verified'] and not report['listing_is_authoritative']


def test_bundle_stays_private_when_its_lock_is_created_first(saved):
    _,bundle,_=saved
    assert bundle.stat().st_mode & 0o777 == 0o700


def test_manifest_bytes_are_addressed_without_silent_reformatting(saved):
    _,bundle,snapshot=saved
    path=DirectoryStore(bundle).path(handoff.manifest_key(snapshot))
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='manifest identity'):handoff.load_manifest(bundle,snapshot)
    with pytest.raises(ValueError,match='manifest identity'):handoff.list_snapshots(DirectoryStore(bundle))


def test_explicit_repair_quarantines_bad_bytes_and_never_automatically_overwrites(saved,tmp_path):
    _,bundle,snapshot=saved;remote=DirectoryStore(tmp_path/'remote')
    handoff.deliver(bundle,snapshot,remote)
    member=handoff.load_manifest(bundle,snapshot)['files'][0];key=handoff.object_key(member['sha256'])
    remote.path(key).write_bytes(b'damaged replica')
    with pytest.raises(ValueError):handoff.deliver(bundle,snapshot,remote)
    assert remote.path(key).read_bytes()==b'damaged replica'
    result=handoff.repair(bundle,snapshot,remote,member['sha256'])
    assert result['state']=='repaired'
    assert next((bundle/'quarantine').rglob('damaged-object')).read_bytes()==b'damaged replica'
    assert handoff.deliver(bundle,snapshot,remote)['uploaded_objects']==0
    remote.path(handoff.manifest_key(snapshot)).write_bytes(b'bad manifest')
    assert handoff.repair(bundle,snapshot,remote)['state']=='repaired'
    assert handoff.repair(bundle,snapshot,remote)['state']=='already_valid'


def test_failed_repair_keeps_quarantine_and_no_success_receipt(saved,tmp_path,monkeypatch):
    _,bundle,snapshot=saved;remote=DirectoryStore(tmp_path/'remote');handoff.deliver(bundle,snapshot,remote)
    member=handoff.load_manifest(bundle,snapshot)['files'][0];key=handoff.object_key(member['sha256'])
    remote.path(key).write_bytes(b'bad')
    def outage(*args):raise OSError('repair outage')
    monkeypatch.setattr(remote,'replace_file',outage)
    with pytest.raises(OSError):handoff.repair(bundle,snapshot,remote,member['sha256'])
    assert remote.path(key).read_bytes()==b'bad'
    assert next((bundle/'quarantine').rglob('damaged-object')).read_bytes()==b'bad'
    assert not (bundle/'repairs').exists()


def test_receipt_rejects_filesystem_or_other_bucket(saved,tmp_path):
    _,bundle,snapshot=saved;root=tmp_path/'restored';handoff.restore(bundle,snapshot,root)
    receipt=handoff.deliver(bundle,snapshot,DirectoryStore(tmp_path/'mirror'))
    path=tmp_path/'receipt.json';handoff.journal(path,receipt)
    expected='s3:fixture.invalid/evidence/facts-v1'
    with pytest.raises(ValueError,match='destination'):
        postgres_sync.validate_delivery(root,bundle,snapshot,path,expected)
    receipt['destination']=expected+'/wrong';handoff.journal(path,receipt)
    with pytest.raises(ValueError,match='destination'):
        postgres_sync.validate_delivery(root,bundle,snapshot,path,expected)
    receipt['destination']=expected;handoff.journal(path,receipt)
    postgres_sync.validate_delivery(root,bundle,snapshot,path,expected)


def test_unlinked_cli_import_rejected_before_opening_catalog(monkeypatch,tmp_path):
    monkeypatch.setattr('sys.argv',['postgres_sync','--apply','--root',str(tmp_path/'absent')])
    with pytest.raises(SystemExit) as e:postgres_sync.main()
    assert e.value.code==2 and not (tmp_path/'absent').exists()


def test_unavailable_remote_manifest_blocks_database_connection(saved,tmp_path,monkeypatch):
    _,bundle,snapshot=saved;root=tmp_path/'restored';handoff.restore(bundle,snapshot,root)
    expected='s3:fixture.invalid/evidence/facts-v1'
    receipt=tmp_path/'receipt.json'
    handoff.journal(receipt,{'snapshot':snapshot,'destination':expected,'verified_at':'2026-10-25T00:00:00Z',
        'verified_objects':len({f['sha256'] for f in handoff.load_manifest(bundle,snapshot)['files']})})
    for key,value in {'FACTS_S3_ENDPOINT':'https://fixture.invalid','FACTS_S3_BUCKET':'evidence',
            'FACTS_S3_ACCESS_KEY_ID':'fixture','FACTS_S3_SECRET_ACCESS_KEY':'fixture'}.items():
        monkeypatch.setenv(key,value)
    def outage(*args):raise OSError('replica unavailable')
    def forbidden(*args,**kwargs):raise AssertionError('Opened database despite inaccessible replica')
    monkeypatch.setattr(S3Store,'open',outage);monkeypatch.setattr('psycopg.connect',forbidden)
    monkeypatch.setattr('sys.argv',['postgres_sync','--apply','--root',str(root),'--snapshot',snapshot,
        '--bundle',str(bundle),'--artifact-receipt',str(receipt)])
    with pytest.raises(SystemExit,match='OSError'):postgres_sync.main()
    # Failed verification releases ownership for a subsequent supported writer.
    with writer_lock(root): pass


def test_writer_lock_rejects_another_process_before_backup_mutates(saved):
    root,bundle,_=saved
    script='from facts.handoff import backup; import sys; backup(sys.argv[1],sys.argv[2])'
    for target in [root,bundle]:
        with writer_lock(target):
            # Nested same-thread operations remain usable.
            with writer_lock(target):pass
            result=subprocess.run([sys.executable,'-c',script,str(root),str(bundle)],capture_output=True,text=True)
        assert result.returncode!=0 and 'Another collector/backup writer' in result.stderr


def test_s3_snapshot_listing_paginates_within_its_own_prefix():
    store=S3Store({'FACTS_S3_ENDPOINT':'https://fixture.invalid','FACTS_S3_BUCKET':'evidence',
        'FACTS_S3_ACCESS_KEY_ID':'fixture','FACTS_S3_SECRET_ACCESS_KEY':'fixture'})
    prefix='facts-v1/manifests/'
    with Stubber(store.client) as stub:
        stub.add_response('list_objects_v2',{'IsTruncated':True,'NextContinuationToken':'next',
            'Contents':[{'Key':prefix+'a'*64+'.json'}]}, {'Bucket':'evidence','Prefix':prefix})
        stub.add_response('list_objects_v2',{'IsTruncated':False,
            'Contents':[{'Key':prefix+'b'*64+'.json'}]}, {'Bucket':'evidence','Prefix':prefix,'ContinuationToken':'next'})
        assert list(store.keys('manifests'))==['manifests/'+'a'*64+'.json','manifests/'+'b'*64+'.json']


def test_destination_identity_preserves_endpoint_port_and_rejects_path_aliases():
    values={'FACTS_S3_ENDPOINT':'https://fixture.invalid','FACTS_S3_BUCKET':'evidence',
        'FACTS_S3_ACCESS_KEY_ID':'fixture','FACTS_S3_SECRET_ACCESS_KEY':'fixture'}
    a=S3Store(values)
    b=S3Store({**values,'FACTS_S3_ENDPOINT':'https://fixture.invalid:8443'})
    assert a.identity!=b.identity
    with pytest.raises(ValueError):S3Store({**values,'FACTS_S3_ENDPOINT':'https://fixture.invalid/other-origin'})


def test_remote_verify_cannot_silently_verify_only_local_bundle(monkeypatch):
    monkeypatch.setattr('sys.argv',['facts-handoff','verify','--remote','--snapshot','a'*64])
    with pytest.raises(SystemExit) as error:handoff.main()
    assert error.value.code==2
