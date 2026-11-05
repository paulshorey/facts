"""Bounded paid extraction. Reservations survive interruption; retries are explicit."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from decimal import Decimal
import json
import os
from uuid import uuid4

from . import core


def amount(value, name, zero=False):
    try: value = Decimal(str(value))
    except Exception: raise ValueError('Configure '+name) from None
    if not value.is_finite() or value < 0 or (not zero and value == 0):
        raise ValueError('Invalid '+name)
    return value


def reservation(body, input_rate, output_rate):
    # UTF-8 byte count deliberately overestimates ordinary text tokenization;
    # extra framing and reasoning allowance is conservative, not a provider quote.
    prompt_bound = len(core.dumps(body).encode()) + 4096
    return Decimal(3) * (prompt_bound*input_rate + (body['max_tokens']+4096)*output_rate) / 1000000


def collect(store, topic, limit, workers, *, maximum, input_rate, output_rate, retry_failed, existing_only):
    if topic == 'all' or not topic or limit <= 0 or workers not in range(1,9):
        raise ValueError('Extraction requires one topic, a positive limit and one through eight workers')
    ceiling = amount(maximum, 'extraction dollar ceiling', zero=True)
    input_rate = amount(input_rate if input_rate is not None else os.getenv('FACTS_EXTRACT_INPUT_RATE'), 'input USD per million tokens')
    output_rate = amount(output_rate if output_rate is not None else os.getenv('FACTS_EXTRACT_OUTPUT_RATE'), 'output USD per million tokens')
    if ceiling > 0 and not os.getenv('FIREWORKS_API_KEY'):
        raise ValueError('Configure provider credentials before extraction')
    if not existing_only: core.queue_extraction(store, topic)
    states = "('queued','failed')" if retry_failed else "('queued')"
    rows = iter(store.db.execute(f'''SELECT j.*,t.payload topic_payload,c.source FROM extraction_jobs j
        JOIN topics t ON t.id=j.topic JOIN captures c ON c.id=j.capture
        WHERE j.topic=? AND j.state IN {states} AND j.attempts<2 ORDER BY j.chunk,j.rowid LIMIT ?''', (topic,limit)).fetchall())
    report = {'id':uuid4().hex,'topic':topic,'model':core.MODEL,'maximum_usd':str(ceiling),
              'input_usd_per_million':str(input_rate),'output_usd_per_million':str(output_rate),
              'scheduled':0,'completed':0,'reserved_usd':'0','state':'finished',
              'pricing_basis':'operator-configured rates; conservative reservation for up to three HTTP attempts'}
    reserved = Decimal(0); pending = {}; stopped = False; exhausted = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            while pending or not exhausted:
                while not stopped and not exhausted and len(pending)<workers:
                    row = next(rows, None)
                    if row is None: exhausted=True; break
                    text=store.read(row['text_artifact'])
                    context={k:v for k,v in json.loads(row['topic_payload']).items() if k!='searches'}
                    body=core.extraction_body(text,context)
                    charge=reservation(body,input_rate,output_rate)
                    if reserved+charge>ceiling:
                        report['state']='budget_limited';stopped=True;break
                    # Commit cost + running state before the request can be sent.
                    # A crash/failed save leaves an uncertain running job, never an
                    # automatically resubmitted queued/failed job.
                    with store.db:
                        store.attempt('normalize_reservation',row['id'],'reserved',metadata={
                            'run_id':report['id'],'reserved_usd':str(charge),'model':core.MODEL,
                            'input_usd_per_million':str(input_rate),'output_usd_per_million':str(output_rate),
                            'request_digest':core.ident('extraction-request',body)})
                        store.db.execute("UPDATE extraction_jobs SET state='running',attempts=attempts+1 WHERE id=?",(row['id'],))
                    reserved+=charge;report['reserved_usd']=str(reserved);report['scheduled']+=1
                    pending[pool.submit(core.llm_extract,text,context,body=body)]=row
                if not pending: break
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    row=pending.pop(future)
                    uncertain = False
                    try: raw,parsed,error=future.result()
                    except Exception as failure:
                        # No response proves completion/rejection. Preserve the running
                        # reservation for manual recovery, including with --retry-failed.
                        uncertain = True; stopped = True; report['state']='provider_uncertain'
                        raw={'error_class':type(failure).__name__};parsed=None;error=type(failure).__name__
                        response=getattr(failure,'response',None)
                        status=getattr(response,'status_code',None)
                        if status in {401,403,429}:
                            # A received auth/rate-limit rejection is a definitive refusal before
                            # inference, not an unknown outcome: requeue the job and stop dispatch.
                            uncertain=False;stopped=True;report['state']='provider_stopped'
                            raw.update(status=status,body=(response.text or '')[:2000],
                                       rate_limit={k:v for k,v in response.headers.items() if 'ratelimit' in k.lower() or k.lower()=='retry-after'})
                            error=f'http_{status}'
                    # Storage/transaction failures propagate out of the dispatcher.
                    # They must not be treated as another provider retry.
                    rejected=error in {'http_401','http_403','http_429'}
                    with store.db:
                        artifact=store.put(raw)
                        if error:
                            split=core.split_job(store,row) if error=='output_truncated' else []
                            state='running' if uncertain else ('queued' if rejected else ('split' if split else 'failed'))
                        else:
                            for index,item in enumerate(parsed.items):
                                store.item(topic,row['source'],row['capture'],item.model_dump(),{'type':'extracted_chunk',
                                    'job_id':row['id'],'chunk':row['chunk'],'ordinal':index,
                                    'prompt_version':core.PROMPT_VERSION,'model':core.MODEL})
                            for query in parsed.followup_queries:
                                store.db.execute('INSERT OR IGNORE INTO followups VALUES (?,?,?)',(topic,query,row['id']))
                            state='succeeded'
                        store.db.execute('UPDATE extraction_jobs SET state=?,artifact=?,error=? WHERE id=?',(state,artifact,error,row['id']))
                        if error and rejected:
                            # A pre-inference refusal is not a paid attempt; keep the job eligible.
                            store.db.execute('UPDATE extraction_jobs SET attempts=max(attempts-1,0) WHERE id=?',(row['id'],))
                        store.attempt('normalize',row['id'],state,artifact,{'usage':raw.get('usage'),'model':core.MODEL,
                            'error':error,'run_id':report['id']})
                    report['completed']+=1
                    print(f"normalize {report['completed']}/{limit} {topic}: {error or str(len(parsed.items))+' items'}",flush=True)
        finally:
            for future in pending: future.cancel()
    artifact=store.put(report)
    store.attempt('normalize_run',report['id'],report['state'],artifact,report);store.db.commit()
    return report
