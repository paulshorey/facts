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


@pytest.mark.parametrize('status', [401, 403, 429])
def test_received_rejection_requeues_job_records_status_and_stops(queue, monkeypatch, status):
    import httpx
    calls=[]
    def request(text, *args, **kwargs):
        calls.append(text)
        response=httpx.Response(status, headers={'x-ratelimit-over-limit':'yes','retry-after':'7'}, text='slow down',
                                request=httpx.Request('POST','https://api.fireworks.ai/inference/v1/chat/completions'))
        raise httpx.HTTPStatusError('rejected', request=response.request, response=response)
    monkeypatch.setattr(core, 'llm_extract', request)
    report=core.normalize(queue, 'topic', 3, 1, maximum=1, input_rate='.15', output_rate='.5', existing_only=True)
    assert report['state'] == 'provider_stopped' and calls == ['source text 1']
    state, error, artifact = queue.db.execute("SELECT state,error,artifact FROM extraction_jobs WHERE id='1'").fetchone()
    assert (state, error) == ('queued', f'http_{status}')
    saved = json.loads((queue.root/artifact).read_text())
    assert saved['status'] == status and saved['body'] == 'slow down' and saved['rate_limit']['retry-after'] == '7'
    assert queue.db.execute("SELECT state FROM extraction_jobs WHERE id='2'").fetchone()[0] == 'queued'


def test_rate_limit_retries_do_not_consume_server_retry_allowance(monkeypatch):
    import httpx
    monkeypatch.setenv('FIREWORKS_API_KEY','fixture-no-network')
    monkeypatch.setattr(core.time, 'sleep', lambda seconds: None)
    statuses=[429,429,429,503,503,200]
    payload={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'items':[{'title':'Saved'}]})}}]}
    def post(self, url, **kwargs):
        code=statuses.pop(0)
        return httpx.Response(code, json=payload if code==200 else {}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.Client, 'post', post)
    data, parsed, error = core.llm_extract('text', {})
    assert error is None and parsed.items[0].title == 'Saved'
    assert [h['status'] for h in data['_collection_request_history']] == [429,429,429,503,503,200]
    statuses[:]=[503,503,503]
    with pytest.raises(httpx.HTTPStatusError):
        core.llm_extract('text', {})


def test_repeated_refusals_do_not_strand_a_job(queue, monkeypatch):
    import httpx
    def refuse(text, *args, **kwargs):
        response=httpx.Response(429, request=httpx.Request('POST','https://api.fireworks.ai/x'))
        raise httpx.HTTPStatusError('rejected', request=response.request, response=response)
    monkeypatch.setattr(core, 'llm_extract', refuse)
    options=dict(maximum=1, input_rate='.15', output_rate='.5', existing_only=True)
    for _ in range(3):
        core.normalize(queue, 'topic', 3, 1, **options)
    assert tuple(queue.db.execute("SELECT state,attempts FROM extraction_jobs WHERE id='1'").fetchone()) == ('queued', 0)
    calls=[]
    def succeed(text, *args, **kwargs):
        calls.append(text); return {}, core.Extraction(items=[]), None
    monkeypatch.setattr(core, 'llm_extract', succeed)
    core.normalize(queue, 'topic', 3, 1, **options)
    assert 'source text 1' in calls


def test_triage_defers_duplicates_datasets_and_short_text_reversibly(tmp_path, monkeypatch):
    from facts import triage
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    body='Long disclosure text. '*40
    def job(url, text, state='queued', dataset=False):
        sid=store.source('topic',url);cid=store.capture(sid,'direct_http',b'raw',text)
        if dataset:
            store.db.execute("INSERT INTO datasets VALUES (?,?,?,?,?)",('dataset_'+url[-1],'topic',sid,store.put('x'),'{}'))
        jid='job_'+url[-1]
        store.db.execute('''INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,state)
            VALUES (?,'topic',?,0,?,'fixture',?)''',(jid,cid,store.put(text),state))
        return jid
    official=job('https://oge.gov/a', body, state='succeeded')
    mirror=job('https://cnbc.com/b', '  '+body.replace(' ', '  '))
    workbook=job('https://omb.gov/c', body+' workbook', dataset=True)
    title=job('https://youtube.com/d', 'A video title')
    unique=job('https://example.com/e', body+' unique')
    store.db.commit()
    decisions=triage.plan(store,'topic')
    assert {k: v[0] for k, v in decisions.items()} == {mirror: 'duplicate_capture_text', workbook: 'structured_dataset',
                                                      title: 'insufficient_text'}
    assert decisions[mirror][1]['kept_capture'] == store.db.execute("SELECT capture FROM extraction_jobs WHERE id=?", (official,)).fetchone()[0]
    triage.apply(store,'topic',decisions)
    states=dict(store.db.execute("SELECT id,state FROM extraction_jobs"))
    assert states[unique]=='queued' and states[mirror]=='deferred' and states[official]=='succeeded'
    assert store.db.execute("SELECT count(*) FROM attempts WHERE stage='triage' AND outcome='deferred'").fetchone()[0]==3
    calls=[]
    monkeypatch.setenv('FIREWORKS_API_KEY','fixture-no-network')
    monkeypatch.setattr(core,'llm_extract',lambda text,*a,**k:(calls.append(text),({},core.Extraction(items=[]),None))[1])
    core.normalize(store,'topic',10,1,maximum=1,input_rate='.15',output_rate='.5',existing_only=True)
    assert calls==[body+' unique']
    assert triage.restore(store,'topic','insufficient_text')==1
    assert store.db.execute("SELECT state FROM extraction_jobs WHERE id=?",(title,)).fetchone()[0]=='queued'


def test_uncertain_run_state_survives_a_later_refusal_in_the_same_batch(queue, monkeypatch):
    import httpx
    from facts import extraction
    order=[]
    def request(text, *args, **kwargs):
        order.append(text)
        if text=='source text 1': raise TimeoutError('lost')
        response=httpx.Response(429, request=httpx.Request('POST','https://api.fireworks.ai/x'))
        raise httpx.HTTPStatusError('rejected', request=response.request, response=response)
    monkeypatch.setattr(core, 'llm_extract', request)
    real_wait=extraction.wait
    def both_done(pending, return_when):
        real_wait(pending, return_when='ALL_COMPLETED')
        # Deliver the ambiguous failure before the refusal.
        return sorted(pending, key=lambda f: pending[f]['id']), set()
    monkeypatch.setattr(extraction, 'wait', both_done)
    report=core.normalize(queue, 'topic', 3, 2, maximum=1, input_rate='.15', output_rate='.5', existing_only=True)
    assert report['state']=='provider_uncertain'
    states=dict(queue.db.execute("SELECT id,state FROM extraction_jobs WHERE id IN ('1','2')"))
    assert states=={'1':'running','2':'queued'}


def test_triage_keeps_duplicate_chunks_the_kept_capture_no_longer_covers(tmp_path):
    from facts import triage
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    body='Long disclosure text. '*40
    jobs={}
    for name,states in [('a',['succeeded','failed']),('b',['queued','queued'])]:
        sid=store.source('topic','https://example.com/'+name);cid=store.capture(sid,'direct_http',b'raw',body)
        for chunk,state in enumerate(states):
            jid=f'{name}{chunk}';jobs[jid]=state
            store.db.execute('''INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,state)
                VALUES (?,'topic',?,?,?,'fixture',?)''',(jid,cid,chunk,store.put(body+str(chunk)),state))
    store.db.commit()
    decisions=triage.plan(store,'topic')
    # Capture b covers both chunks, so it is kept and nothing of it is deferred.
    assert decisions=={}
    store.db.execute("UPDATE extraction_jobs SET state='queued' WHERE id='a1'")
    store.db.execute("UPDATE extraction_jobs SET state='failed' WHERE id='b0'");store.db.commit()
    # a covers both chunks (and has completed work); b's chunk 1 is deferred, not its uncovered chunk 0.
    assert set(triage.plan(store,'topic')) == {'b1'}


def test_triage_compares_duplicate_chunk_text_not_indexes(tmp_path):
    from facts import triage
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    words=['word%d' % i for i in range(300)]
    def capture(name, text, chunks):
        sid=store.source('topic','https://example.com/'+name);cid=store.capture(sid,'direct_http',b'raw',text)
        for index,(part,state) in enumerate(chunks):
            store.db.execute('''INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,state)
                VALUES (?,'topic',?,?,?,'fixture',?)''',(f'{name}{index}',cid,index,store.put(part),state))
    thirds=[' '.join(words[i:i+100]) for i in (0,100,200)]
    halves=[' '.join(words[:150]), ' '.join(words[150:])]
    capture('expanded', '  '.join(words), [(thirds[0],'succeeded'),(thirds[1],'succeeded'),(thirds[2],'failed')])
    capture('compact', ' '.join(words), [(halves[0],'queued'),(halves[1],'queued')])
    store.db.commit()
    # Same normalized document, different boundaries: nothing in the compact copy is proven covered.
    assert triage.plan(store,'topic')=={}


def test_triage_preview_does_not_write_the_catalog(tmp_path, monkeypatch):
    from facts import cli, triage
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')");store.db.commit();store.db.close()
    (tmp_path/'topics').mkdir(exist_ok=True)
    (tmp_path/'topics'/'new.json').write_text('{"slug":"new","searches":[]}')
    monkeypatch.setattr('facts.core.ROOT', tmp_path)
    before=(tmp_path/'storage/catalog.sqlite').read_bytes()
    monkeypatch.setattr('sys.argv',['facts','triage','--topic','topic'])
    cli.main()
    assert (tmp_path/'storage/catalog.sqlite').read_bytes()==before
    import sqlite3
    with sqlite3.connect(tmp_path/'storage/catalog.sqlite') as db:
        assert [r[0] for r in db.execute("SELECT id FROM topics")]==['topic']


def test_connection_failures_are_retried_then_requeued_not_uncertain(queue, monkeypatch):
    import httpx
    monkeypatch.setattr(core.time, 'sleep', lambda seconds: None)
    attempts=[]
    def post(self, url, **kwargs):
        attempts.append(url); raise httpx.ConnectTimeout('no route')
    monkeypatch.setattr(httpx.Client, 'post', post)
    report=core.normalize(queue, 'topic', 3, 1, maximum=1, input_rate='.15', output_rate='.5', existing_only=True)
    assert len(attempts)==5 and report['state']=='provider_unreachable'
    assert tuple(queue.db.execute("SELECT state,error,attempts FROM extraction_jobs WHERE id='1'").fetchone())==('queued','connect_failed',0)


def test_triage_keeps_short_substantive_text_and_ignores_failed_split_trees(tmp_path):
    from facts import triage
    store=core.Store(tmp_path);store.db.execute("INSERT INTO topics VALUES ('topic','{}')")
    def add(url, text, jobs):
        sid=store.source('topic',url);cid=store.capture(sid,'search_excerpt',b'raw',text)
        for jid,chunk,state,part in jobs:
            store.db.execute('''INSERT INTO extraction_jobs(id,topic,capture,chunk,text_artifact,prompt_version,state)
                VALUES (?,'topic',?,?,?,'fixture',?)''',(jid,cid,chunk,store.put(part),state))
        return cid
    add('https://example.com/headline','Fund got $2 billion from MGX, filing says',[('extract_h',0,'queued','Fund got $2 billion from MGX, filing says')])
    add('https://example.com/thumb','▶ 0:42',[('extract_t',0,'queued','▶ 0:42')])
    body='Disclosure row. '*60
    # The keeper's only chunk split, and one child failed: it no longer covers that text.
    keeper=add('https://example.com/a',body,[('extract_a',0,'split',body),('extract-part_a1',0,'succeeded','x'),
                                             ('extract-part_a2',0,'failed','y')])
    store.attempt('split_extraction','extract_a','succeeded',metadata={'children':[{'job_id':'extract-part_a1'},{'job_id':'extract-part_a2'}]})
    add('https://example.com/b',body,[('extract_b',0,'queued',body)])
    store.db.commit()
    decisions=triage.plan(store,'topic')
    assert {k:v[0] for k,v in decisions.items()}=={'extract_t':'insufficient_text'}
    store.db.execute("UPDATE extraction_jobs SET state='queued' WHERE id='extract-part_a2'");store.db.commit()
    assert 'extract_b' in triage.plan(store,'topic')


def test_conflicting_mutating_flags_fail_before_any_work(monkeypatch):
    from facts import cli
    for argv in (['facts','triage','--topic','t','--apply','--restore'],):
        monkeypatch.setattr('sys.argv',argv)
        with pytest.raises(SystemExit) as error: cli.main()
        assert error.value.code==2
    from facts import postgres_sync
    monkeypatch.setattr('sys.argv',['facts.postgres_sync','--migrate','--migration-status'])
    with pytest.raises(SystemExit) as error: postgres_sync.main()
    assert error.value.code==2
