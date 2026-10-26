from decimal import Decimal
import json
import sqlite3

import pytest

from facts import core
from facts.extraction import reservation


@pytest.fixture
def queue(tmp_path,monkeypatch):
    monkeypatch.setenv('FIREWORKS_API_KEY','fixture-no-network')
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    sid=store.source('topic','https://example.com/source')
    cid=store.capture(sid,'direct_http',b'source','source text')
    for i in range(3):
        store.db.execute('''INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,state,attempts)
            VALUES (?,'topic',?,?,?,'fixture',?,?)''',(str(i),cid,i,store.put('source text '+str(i)),
                'failed' if i==0 else 'queued',1 if i==0 else 0))
    store.db.commit();return store


def test_budget_caps_parallel_submission_and_reserves_before_request(queue,monkeypatch):
    calls=[]
    def request(text,topic,*,body):
        with sqlite3.connect(queue.root/'storage/catalog.sqlite') as reader:
            assert reader.execute("SELECT state FROM extraction_jobs WHERE id=?",(text[-1],)).fetchone()[0]=='running'
            assert reader.execute("SELECT count(*) FROM attempts WHERE stage='normalize_reservation'").fetchone()[0]==1
        calls.append(text)
        return {'usage':{'prompt_tokens':10,'completion_tokens':20}},core.Extraction(items=[{'title':'Saved fact'}]),None
    monkeypatch.setattr(core,'llm_extract',request)
    charge=reservation(core.extraction_body('source text 1',{}),Decimal('.15'),Decimal('.5'))
    report=core.normalize(queue,'topic',3,4,maximum=charge*Decimal('1.5'),input_rate='.15',output_rate='.5',existing_only=True)
    assert calls==['source text 1'] and report['state']=='budget_limited'
    assert Decimal(report['reserved_usd'])<=Decimal(report['maximum_usd'])
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='0'").fetchone()[0]=='failed'
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='2'").fetchone()[0]=='queued'


def test_storage_failure_stops_dispatch_and_does_not_requeue_uncertain_job(queue,monkeypatch):
    calls=[]
    def request(*args,**kwargs):
        calls.append(True);return {},core.Extraction(items=[]),None
    def disk_failure(*args,**kwargs):raise OSError('simulated disk failure')
    monkeypatch.setattr(core,'llm_extract',request);monkeypatch.setattr(queue,'put',disk_failure)
    with pytest.raises(OSError):
        core.normalize(queue,'topic',3,1,maximum=1,input_rate='.15',output_rate='.5',existing_only=True)
    assert len(calls)==1 and not queue.db.in_transaction
    assert tuple(queue.db.execute("SELECT state,attempts FROM extraction_jobs WHERE id='1'").fetchone())==('running',1)
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='2'").fetchone()[0]=='queued'
    assert queue.db.execute("SELECT count(*) FROM attempts WHERE stage='normalize_reservation'").fetchone()[0]==1


@pytest.mark.parametrize('maximum,input_rate',[(None,'.15'),(-1,'.15'),('NaN','.15'),(1,None),(1,0)])
def test_missing_or_invalid_budget_and_rates_fail_before_queue_changes(queue,maximum,input_rate,monkeypatch):
    monkeypatch.delenv('FACTS_EXTRACT_INPUT_RATE',raising=False)
    before=queue.db.execute('SELECT count(*) FROM attempts').fetchone()[0]
    with pytest.raises(ValueError):
        core.normalize(queue,'topic',1,1,maximum=maximum,input_rate=input_rate,output_rate='.5',existing_only=True)
    assert queue.db.execute('SELECT count(*) FROM attempts').fetchone()[0]==before


def test_retry_failed_requires_explicit_option(queue,monkeypatch):
    calls=[]
    def request(text,*args,**kwargs):
        calls.append(text);return {},core.Extraction(items=[]),None
    monkeypatch.setattr(core,'llm_extract',request)
    core.normalize(queue,'topic',1,1,maximum=1,input_rate='.15',output_rate='.5',existing_only=True,retry_failed=True)
    assert calls==['source text 0']


def test_cli_rejects_implicit_budget_and_topic_before_store_initialization(monkeypatch):
    from facts import cli
    monkeypatch.setattr('sys.argv',['facts','normalize'])
    def forbidden(*args):raise AssertionError('CLI initialized the live store')
    monkeypatch.setattr(cli,'Store',forbidden)
    with pytest.raises(SystemExit) as error:cli.main()
    assert error.value.code==2


@pytest.mark.parametrize('failure', [TimeoutError('response lost'), ValueError('malformed response')])
def test_ambiguous_provider_failure_remains_running_even_with_explicit_retry(queue, monkeypatch, failure):
    calls=[]
    def request(text, *args, **kwargs):
        calls.append(text)
        if text == 'source text 1': raise failure
        return {}, core.Extraction(items=[]), None
    monkeypatch.setattr(core, 'llm_extract', request)
    options=dict(maximum=1, input_rate='.15', output_rate='.5', existing_only=True)
    report=core.normalize(queue, 'topic', 3, 1, **options)
    assert tuple(queue.db.execute("SELECT state,attempts FROM extraction_jobs WHERE id='1'").fetchone()) == ('running', 1)
    assert report['state'] == 'provider_uncertain'
    assert calls == ['source text 1']
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='2'").fetchone()[0] == 'queued'
    assert queue.db.execute("SELECT count(*) FROM attempts WHERE stage='normalize_reservation'").fetchone()[0] == 1
    core.normalize(queue, 'topic', 3, 1, retry_failed=True, **options)
    assert calls == ['source text 1', 'source text 0', 'source text 2']
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='1'").fetchone()[0] == 'running'
