# facts.news collector

Read `README.md` and the current topic definitions before running collection.
The user requested broad itemized data gathering, not editorial analysis.

- Keep raw source data, model outputs, credentials and runtime state out of Git.
- Use `.venv/bin/python` and `.venv/bin/facts`; system Python lacks project dependencies.
- Preserve the existing catalog and raw artifacts. Continue pending batches instead
  of deleting data or blindly reissuing external deep-research jobs.
- `storage/deep-research/*-launch.json` records the submitted external task IDs.
- Do not run two passes of the same stage/topic concurrently. Local SQLite WAL
  permits distinct stages but is not a distributed worker lease implementation.
- Store allegations, estimates and quoted descriptions as supplied observations.
  Do not silently convert them into conclusions or require editorial approval to save them.
- Keep dataset units, fiscal periods, account hierarchy, original rows, and citations.
- Counts of observations are not counts of unique events or additive budget totals.
- Remote database writes use `FACTS_DATABASE_URL` and the explicit sync command.
  Never print the connection URL or embed credentials in files/arguments.
- Run the relevant tests and update the README when changing collection behavior.
