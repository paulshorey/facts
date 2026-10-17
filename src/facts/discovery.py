"""Persistent link following and follow-up query expansion."""
import json
import re
from urllib.parse import urljoin, urlsplit

from .core import canonical_url, dumps, ident


def follow_links(store, topic, limit=150):
    rows = store.db.execute("SELECT DISTINCT s.id,s.url,c.artifact,c.text_artifact,c.metadata FROM sources s JOIN topic_sources t ON t.source=s.id JOIN captures c ON c.source=s.id WHERE t.topic=? AND c.level IN ('direct_http','provider_extract') AND c.state='succeeded'", (topic,)).fetchall()
    found = 0
    for row in rows:
        links = []
        metadata = json.loads(row['metadata'])
        if metadata.get('format') == 'html':
            try:
                from lxml import html
                page = html.fromstring((store.root / row['artifact']).read_bytes())
                links = [(urljoin(row['url'], e.get('href')), ' '.join(e.itertext()).strip()) for e in page.xpath('//a[@href]')]
            except Exception:
                pass
        elif row['text_artifact']:
            links = [(urljoin(row['url'], url), title) for title,url in re.findall(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', store.read(row['text_artifact']))]
        for url, title in links:
            if found >= limit:
                break
            if len(title) < 12 or any(t in url.lower() for t in ['/login','/subscribe','/privacy','/terms','/author/','/tag/','/category/','facebook.com','instagram.com','mailto:']):
                continue
            host = (urlsplit(url).hostname or '').lower()
            useful = any(t in host for t in ['reuters.com','apnews.com','nytimes.com','washingtonpost.com','propublica.org','citizen.org','citizensforethics.org','gao.gov','cbo.gov','treasury.gov','whitehouse.gov','congress.gov','house.gov','senate.gov','usaspending.gov']) or url.lower().endswith(('.pdf','.xlsx','.csv','.json'))
            if not useful:
                continue
            try:
                canonical = canonical_url(url)
                exists = store.db.execute('SELECT 1 FROM sources WHERE url=?',(canonical,)).fetchone()
                sid = store.source(topic,canonical,title,family='linked-document')
                if not exists:
                    store.attempt('discover_link',sid,'succeeded',metadata={'parent_source':row['id'],'anchor':title})
                    found += 1
            except ValueError:
                continue
    store.db.commit()
    print(f'{topic}: queued {found} new linked sources')


def expand_followups(store, topic, limit=15):
    rows=store.db.execute('SELECT query,origin FROM followups WHERE topic=? ORDER BY rowid LIMIT ?', (topic,limit)).fetchall()
    for row in rows:
        payload={'family':'followup','objective':f'Collect concrete original sources, itemized examples and datasets for {topic}: {row[0]}. Evidence cutoff 2026-10-16.', 'search_queries':[row[0]]}
        qid=ident('search',topic,payload)
        store.db.execute('INSERT OR IGNORE INTO searches(id,topic,family,payload) VALUES (?,?,?,?)',(qid,topic,'followup',dumps(payload)))
    store.db.commit()
    print(f'{topic}: prepared {len(rows)} follow-up searches')


if __name__ == '__main__':
    import argparse
    from .core import Store
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['links','followups']);p.add_argument('--topic',required=True);p.add_argument('--limit',type=int,default=100)
    a=p.parse_args();s=Store()
    (follow_links if a.mode=='links' else expand_followups)(s,a.topic,a.limit)
