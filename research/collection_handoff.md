# Initial collection handoff

Research cutoff: 2026-10-16. Data collection and source organization only.

| Collection | Source URLs | Item observations excluding headlines | Headline/source entries | Dataset tables | Retained table rows |
| --- | ---: | ---: | ---: | ---: | ---: |
| trump-second-term | 763 | 1758 | 785 | 0 | 0 |
| us-federal-spending | 835 | 8770 | 793 | 88 | 35508 |

There are 1598 distinct cataloged source URLs across both topics. 614 URLs have a successful readable direct/provider capture. The rest include search excerpts, imported source material, dataset files, and pending source leads.

Counts are observations rather than unique incidents. Table rows include source headings and multiple periods; expense amounts are not summed across overlapping rows.

## Open the collections

- Trump items: `exports/trump-second-term/items.jsonl`
- Trump source index: `exports/trump-second-term/sources.jsonl`
- Trump readable inventory: `exports/trump-second-term/inventory.md`
- Spending items: `exports/us-federal-spending/items.jsonl`
- Spending source index: `exports/us-federal-spending/sources.jsonl`
- Spending readable inventory: `exports/us-federal-spending/inventory.md`
- Dataset manifests and Parquet pointers: `exports/us-federal-spending/datasets.jsonl`
- Local catalog: `storage/catalog.sqlite`
- Original captures and provider/model outputs: `storage/objects/`
- Original search campaigns and supplements: `storage/bootstrap/`
- Last30days raw results and source outcomes: `storage/last30days/`
- Completed asynchronous research results: `storage/deep-research/`

## Execution and remaining work

Completed 84 main search requests plus 20 isolated preflight/supplement requests. Both asynchronous research jobs completed and their 168 structured items were imported. Last30days returned Reddit/Hacker News items and some Trump-topic YouTube data; the spending YouTube lane timed out, and a separate yt-dlp metadata collection preserved 30 additional video candidates. Optional skill reranking returned HTTP 401 and used its local fallback. Source-level partial outcomes remain in the raw files.

The bounded extraction and recovery passes completed. More captured chunks and linked URLs remain queued. No recurring automation or continuously running collector is installed. Rerun the documented stage commands to expand the collection. CBO direct spreadsheet downloads returned HTTP 403; other successful routes and the discovered URLs are retained.

A PostgreSQL sync dry-run is saved at `storage/postgres-sync-dry-run.json`. Supply the development connection as `FACTS_DATABASE_URL` to use the explicit apply command. Raw artifacts remain local; remote object storage has not been provisioned.

Only tooling, topic definitions, and these small handoff summaries are in Git. The original research corpus stays in the local ignored storage/export directories.
