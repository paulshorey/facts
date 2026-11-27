# Project: facts.news

A local-first collector for itemized, source-backed research. Read [README.md](README.md)
and current topic definitions before collecting. Save supplied allegations,
estimates, and quotations as observations without an editorial approval gate;
do not convert them into conclusions.

## Workspace

Use a new branch in a task worktree for every change. Open a ready PR whose
description gives the next engineer the context, checks, and remaining work.

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

If something goes wrong, stop the affected operation and report the relevant findings,
data, and logs. Redact secrets and private content.
When unsure, research before deciding: search the web for current techniques and best practices.

# Improve your instructions

Before you finish, note where you struggled and which instructions, tools, or skills
were missing or wrong. Record project lessons in this file or the project docs.
Put lessons that apply across projects in `~/.agents` when you have access. Keep edits
short and specific, and propose larger changes in your reply. If you cannot edit the
relevant files, suggest those changes in your reply.

# Next steps

Consider what was recently done, what is in progress, and how it serves the goal.
Decide and recommend what to do next.

**Goal:** Build a traceable, reusable collection of factual observations.
