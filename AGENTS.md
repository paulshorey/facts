# Project: facts.news

A local-first collector for itemized, source-backed research. Read [README.md](README.md)
and current topic definitions before collecting; gather observations without
turning supplied allegations, estimates, or quotations into editorial conclusions.

## Collection and storage

- Use `.venv/bin/python` and `.venv/bin/facts`; system Python lacks dependencies.
- Keep raw captures, model outputs, credentials, and runtime state out of Git.
  Preserve the catalog and artifacts; continue pending batches instead of restarting.
- External task IDs live in `storage/deep-research/*-launch.json`. Reconcile uncertain
  jobs manually before resubmitting. Paid extraction needs an explicit topic, job
  limit, dollar ceiling, and verified current model token rates.
- Use one writer per collector root/host, across topics, stages, and backups.
  SQLite WAL and file locks are not distributed worker leases.
- Preserve units, fiscal periods, account hierarchy, original rows, and citations.
  Observation counts are not unique-event counts or additive budget totals.
- Remote writes use `FACTS_DATABASE_URL` and explicit sync. Require a verified
  snapshot and the configured S3 delivery receipt; never advance an unlinked mirror.
  Never print connection URLs or put credentials in files or command arguments.

## Validation

Run relevant tests and update README.md when changing collection behavior.

# Research and report

If something goes wrong, stop the affected operation and report relevant findings, data, and logs.
Redact secrets and private content.
If unsure about something, research, search the web, find latest techniques and best practices.

# Next steps

Consider what was recently done, what's in progress, and how it contributes to the goal.
Decide and recommend what to do next.

# Goal: Build a traceable, reusable collection of factual observations
