# Project: facts.news

A local-first collector for itemized, source-backed research. Read [README.md](README.md)
and current topic definitions before collecting. Save supplied allegations,
estimates, and quotations as observations without an editorial approval gate;
do not convert them into conclusions.

## Workspace

Prefer the primary local checkout when free; use a worktree when another task is
using it. After merging a PR, return the primary checkout to `main` and run
`git pull --ff-only` when free, leaving it clean.

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

# Next steps

Consider what was recently done, what is in progress, and how it serves the goal.
Decide and recommend what to do next.

# Improve your instructions

Before you finish, look back at how the work went: where you struggled, which
troubleshooting took longest, and which instructions, tools, or skills were missing,
wrong, or unclear. Fix what would help the next agent. Put project lessons in this
file or the project's docs, and lessons for every project in the shared skills at
`~/.agents/skills/` (linked at `~/.claude/skills/`); `~/.agents/AGENTS.md` explains
how to save those. Keep each edit short and specific, tell the user what you changed,
and propose larger changes instead of making them. If you cannot reach `~/.agents`,
for example as a cloud agent or a read-only reviewer, suggest the improvements in your
reply instead of editing files.

# Goal: Build a traceable, reusable collection of factual observations
