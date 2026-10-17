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
