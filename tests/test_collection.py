import json
from pathlib import Path

import pytest

from facts.core import Store, canonical_url, chunks, export, import_packet, validate_public_url


def test_idempotent_observations_keep_differing_figures(tmp_path):
    s=Store(tmp_path)
    sid=s.source('topic','https://example.com/report?utm_source=x','Report')
    cid=s.capture(sid,'search_excerpt',{'value':0},'The number is zero.')
    s.item('topic',sid,cid,{'kind':'statistic','title':'Zero','value':0,'missing':None},{'row':1})
    s.item('topic',sid,cid,{'kind':'statistic','title':'Zero','value':0,'missing':None},{'row':1})
    s.item('topic',sid,cid,{'kind':'statistic','title':'Other report','value':False},{'row':2})
    assert s.db.execute('select count(*) from items').fetchone()[0]==2
    payload=json.loads(s.db.execute('select payload from items order by rowid').fetchone()[0])
    assert payload['value']==0 and payload['missing'] is None
    assert payload['references'][0]['url']=='https://example.com/report'


def test_chunking_covers_input_without_gaps():
    text='word '*10000
    parts=list(chunks(text,1500))
    assert parts[0][0]==0 and parts[-1][1]==len(text)
    for prior,following in zip(parts,parts[1:]):
        assert prior[1]>=following[0] and prior[0]<following[0]


def test_canonical_url_preserves_record_ids():
    assert canonical_url('https://Example.com/a?id=123&utm_campaign=x#section')=='https://example.com/a?id=123'
    with pytest.raises(ValueError):canonical_url('http://user:secret@example.com/a')


def test_private_destinations_rejected(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError):validate_public_url('https://example.com/')


def test_raw_artifact_survives_and_packet_import_repeats_safely(tmp_path):
    s=Store(tmp_path)
    p=tmp_path/'packet.json';p.write_text(json.dumps({'sources':[{'url':'https://example.com/a','title':'A','text':'supplied source excerpt'}]}))
    import_packet(s,'topic',p);import_packet(s,'topic',p)
    assert s.db.execute('select count(*) from items').fetchone()[0]==1
    row=s.db.execute('select * from captures').fetchone()
    assert 'supplied source excerpt' in s.read(row['text_artifact'])


def test_item_export_keeps_references_and_partial_state(tmp_path):
    s=Store(tmp_path)
    s.db.execute('insert into topics values (?,?)',('topic','{}'))
    sid=s.source('topic','https://example.com/a','A')
    cid=s.capture(sid,'search_excerpt',{'text':'A'},'A')
    s.item('topic',sid,cid,{'kind':'headline','title':'A'},{'type':'search_result'})
    s.db.commit();export(s)
    rows=(tmp_path/'exports/topic/items.jsonl').read_text().splitlines()
    assert json.loads(rows[0])['references'][0]['source_id']==sid
    assert json.loads((tmp_path/'exports/topic/manifest.json').read_text())['counts']['sources']==1


def test_workbook_recovers_incorrect_dimensions_and_keeps_all_columns(tmp_path,monkeypatch):
    import io,zipfile
    import openpyxl
    import pyarrow.parquet as pq
    from facts.datasets import workbook
    b=openpyxl.Workbook();w=b.active
    w.append(['Report title']);w.append(['Category','2025','2026']);w.append(['A',0,12]);w.append(['B',None,False])
    original=io.BytesIO();b.save(original)
    modified=io.BytesIO()
    with zipfile.ZipFile(original) as source,zipfile.ZipFile(modified,'w') as target:
        for info in source.infolist():
            data=source.read(info.filename)
            if info.filename=='xl/worksheets/sheet1.xml':
                data=data.replace(b'dimension ref="A1:C4"',b'dimension ref="A1:A1"')
            target.writestr(info,data)
    monkeypatch.setattr('facts.datasets.retrieve',lambda url:(modified.getvalue(),'',{},200))
    s=Store(tmp_path);workbook(s,'https://example.com/table.xlsx')
    d=json.loads(s.db.execute('select payload from datasets').fetchone()[0])
    table=pq.read_table(tmp_path/d['parquet'])
    assert table.num_rows==4 and 'column_3' in table.column_names
    assert table.to_pylist()[2]['column_2']=='0'


def test_truncated_chunk_splits_without_losing_parent_input(tmp_path):
    from facts.core import split_job
    s=Store(tmp_path)
    sid=s.source('topic','https://example.com/report');cid=s.capture(sid,'direct_http',b'raw','original')
    text='word '*2000;artifact=s.put(text)
    parent={'id':'parent','topic':'topic','capture':cid,'chunk':0,'text_artifact':artifact,'prompt_version':'v1'}
    children=split_job(s,parent)
    assert len(children)>1
    assert children[0]['start']==0 and children[-1]['end']==len(text)
    assert s.read(artifact)==text


def test_structured_packet_keeps_missing_citations_and_source_kind(tmp_path):
    s=Store(tmp_path)
    p=tmp_path/'packet.json'
    p.write_text(json.dumps({'items':[{'id':'external','kind':'proposal_estimate','title':'A reported estimate','amount':0,'unit':'USD'}]}))
    import_packet(s,'topic',p);import_packet(s,'topic',p)
    rows=s.db.execute('select payload from items').fetchall()
    assert len(rows)==1
    item=json.loads(rows[0][0])
    assert item['references']==[] and item['data']['amount']==0
    assert item['collection_metadata']['source_item_kind']=='proposal_estimate'
    assert item['kind']=='fact'


def test_structured_packet_links_every_supplied_url_and_reimports_safely(tmp_path):
    s=Store(tmp_path);p=tmp_path/'packet.json'
    p.write_text(json.dumps({'items':[{'id':'one','title':'A transaction','source_urls':['https://example.com/a','https://example.com/b']}]}))
    import_packet(s,'topic',p);import_packet(s,'topic',p)
    rows=s.db.execute('select payload from items').fetchall()
    assert len(rows)==1
    assert len(json.loads(rows[0][0])['references'])==2


def test_capture_pins_validated_ip_and_preserves_host_tls_and_redirect_policy(monkeypatch):
    import httpx
    from facts.core import retrieve
    calls, resolved = [], []
    def dns(host, port, **kwargs):
        resolved.append((host, port))
        address = "127.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, port))]
    monkeypatch.setattr("socket.getaddrinfo", dns)
    def handler(request):
        calls.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/done"})
        if request.url.path == "/private":
            return httpx.Response(302, headers={"location": "http://private.example/"})
        return httpx.Response(200, text="public text", headers={"content-type": "text/plain"})
    client = httpx.Client
    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["limits"].max_keepalive_connections == 0
        return client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr("facts.core.httpx.Client", make_client)
    body, text, metadata, status = retrieve("https://public.example:8443/start")
    assert status == 200 and text == "public text"
    assert metadata["url"] == "https://public.example:8443/done"
    assert all(r.url.host == "93.184.216.34" and r.url.port == 8443 for r in calls)
    assert all(r.headers["host"] == "public.example:8443" and r.extensions["sni_hostname"] == "public.example" for r in calls)
    assert resolved == [("public.example", 8443), ("public.example", 8443)]
    with pytest.raises(ValueError, match="private/local"):
        retrieve("https://public.example/private")
    assert len(calls) == 3  # Redirected private destination never reaches transport.


def test_followup_batches_advance_and_sync_keeps_stable_lead_ids(tmp_path):
    from facts.discovery import expand_followups
    from facts.postgres_sync import records
    s = Store(tmp_path)
    for i in range(5):
        s.db.execute("INSERT INTO followups VALUES (?,?,?)", ("topic", f"lead {i}", "packet"))
    for expected in [2, 4, 5, 5]:
        expand_followups(s, "topic", limit=2)
        assert s.db.execute("SELECT count(*) FROM searches").fetchone()[0] == expected
    before = [(rid, payload) for kind, rid, _, payload in records(s) if kind == "followups"]
    s.db.execute("UPDATE followups SET origin='new packet' WHERE query='lead 0'")
    after = [(rid, payload) for kind, rid, _, payload in records(s) if kind == "followups"]
    assert len(before) == 5 and [r[0] for r in before] == [r[0] for r in after]
    assert before[0][1]["origin"] != after[0][1]["origin"]


def test_packet_retains_followups_alongside_sources_and_items(tmp_path):
    s = Store(tmp_path); p = tmp_path / "packet.json"
    p.write_text(json.dumps({"sources": [{"url": "https://example.com/", "title": "Source"}],
        "items": [{"title": "Observation", "source_url": "https://example.com/"}],
        "followup_queries": [" next lead ", "", None]}))
    import_packet(s, "topic", p); import_packet(s, "topic", p)
    assert s.db.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert s.db.execute("SELECT query FROM followups").fetchone()[0] == "next lead"


def test_treasury_failure_does_not_block_other_snapshots_and_amounts_stay_dollars(tmp_path, monkeypatch):
    import httpx
    from facts.datasets import treasury
    s = Store(tmp_path); calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"meta": {"total-pages": 1}, "data": [{"classification_desc": "Example", "classification_id": "one", "current_fytd_net_outly_amt": "12.50"}]})
    client = httpx.Client
    monkeypatch.setattr("facts.datasets.httpx.Client", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    treasury(s)
    assert len(calls) == 6 and s.db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 5
    assert s.db.execute("SELECT count(*) FROM attempts WHERE outcome='failed'").fetchone()[0] == 1
    for row in s.db.execute("SELECT payload FROM items"):
        measurement = json.loads(row[0])["measurements"][0]
        assert measurement["value"] == "12.50" and measurement["original_unit"] == "USD"


def test_missing_omb_manifest_records_partial_coverage_and_continues_cbo(tmp_path, monkeypatch):
    from facts.datasets import run
    s = Store(tmp_path); calls = []
    monkeypatch.setattr("facts.datasets.treasury", lambda store: None)
    monkeypatch.setattr("facts.datasets.workbook", lambda store, url: calls.append(url))
    run(s, all_omb=True)
    assert len(calls) == 2 and all("cbo.gov" in u for u in calls)
    assert s.db.execute("SELECT outcome FROM attempts WHERE stage='dataset_manifest'").fetchone()[0] == "missing"
