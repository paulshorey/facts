"""Read-only structural checks for corpus refreshes and restored snapshots."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import pyarrow.parquet as pq

from .contracts import Observation, SeriesDefinition
from .core import ROOT, dumps
from .handoff import catalog_artifacts, catalog_fingerprint, journal
from .organize import ADAPTER_VERSION
from .organize_store import OrganizedStore
from .organize_tables import MAPPER_VERSION


def audit(root=ROOT, *, current=False):
    root=Path(root);store=OrganizedStore(root,readonly=True)
    try:
        store.db.execute('BEGIN')
        if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Catalog integrity failed')
        original=catalog_fingerprint(store.db)
        inventory=store.inventory()
        sources={r[0] for r in store.db.execute('SELECT id FROM sources')}
        captures={r[0] for r in store.db.execute('SELECT id FROM captures')}
        paths=catalog_artifacts(store.db);versions=Counter();active=hashlib.sha256();checked=0;series_by_dataset={}
        for row in store.db.execute('SELECT payload FROM organized_records WHERE active=1 ORDER BY id,version'):
            value=json.loads(row[0])
            (SeriesDefinition if value['record_type']=='series' else Observation).model_validate(value)
            processing=value['processing']
            if value['record_type']=='series':
                series_by_dataset.setdefault(value['dataset_id'],set()).add(value['id'])
            version=processing.get('adapter_version') or processing.get('mapper_version')
            versions[version or 'unknown']+=1
            if current and version != (MAPPER_VERSION if value['record_type']=='series' else ADAPTER_VERSION):
                raise ValueError('Active record uses an older normalizer')
            for ref in value.get('references',[]):
                if ref.get('source_id') and ref['source_id'] not in sources: raise ValueError('Missing referenced source')
                if ref.get('capture_id') and ref['capture_id'] not in captures: raise ValueError('Missing referenced capture')
            active.update(dumps(value).encode());active.update(b'\n');checked+=1
        for path in paths:
            selected=root/path
            if not selected.resolve().is_relative_to(root.resolve()) or not selected.is_file():
                raise ValueError('Missing or unsafe artifact reference: '+path)
        values_checked=0;rows_checked=0
        for row in store.db.execute('SELECT payload FROM organized_tables'):
            table=json.loads(row[0])
            values=pq.read_table(root/table['values_artifact'],columns=['series_id'])
            identifiers=set(values.column('series_id').to_pylist())
            if (values.num_rows!=table['value_count'] or len(identifiers)!=table['series_count']
                    or identifiers!=series_by_dataset.get(table['id'],set())):
                raise ValueError('Table value/series linkage mismatch')
            coverage=pq.read_table(root/table['row_coverage_artifact'],columns=['state'])
            if dict(Counter(coverage.column('state').to_pylist()))!=table['row_counts']:
                raise ValueError('Dataset row disposition mismatch')
            values_checked+=values.num_rows;rows_checked+=coverage.num_rows
        return {'original_tables':original,'inventory':inventory,'active_digest':active.hexdigest(),
                'contracts_checked':checked,'normalizers':dict(versions),'artifact_references_checked':len(paths),
                'table_values_checked':values_checked,'table_rows_checked':rows_checked}
    finally: store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--current',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=audit(args.root,current=args.current)
    if args.output: journal(args.output,result)
    print(dumps(result))


if __name__=='__main__':main()
