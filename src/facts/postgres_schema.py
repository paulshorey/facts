"""Ordered, checksummed PostgreSQL migrations for the Facts development database.

Migration 1 is the original lossless record mirror (``facts`` schema). Later
migrations add the typed ``research`` read model that is rebuilt from the same
local catalog snapshot during each verified import. Applied migrations are
immutable: a changed checksum or an unknown newer version stops the import.
"""
from __future__ import annotations

import hashlib

MIRROR_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS facts;
CREATE TABLE IF NOT EXISTS facts.records (
  kind text NOT NULL,
  id text NOT NULL,
  topic text,
  payload jsonb NOT NULL,
  synced_at timestamptz NOT NULL,
  PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS records_topic_kind_idx ON facts.records(topic,kind);
CREATE INDEX IF NOT EXISTS records_payload_gin_idx ON facts.records USING gin(payload);
CREATE TABLE IF NOT EXISTS facts.import_receipts (
  id text PRIMARY KEY, catalog_digest text NOT NULL,
  artifact_manifest text, counts jsonb NOT NULL,
  synchronized_kinds jsonb NOT NULL, committed_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS facts.mirror_state (
  id text PRIMARY KEY CHECK (id='current'),
  receipt_id text NOT NULL REFERENCES facts.import_receipts(id)
);
"""

RESEARCH_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS research;
COMMENT ON SCHEMA research IS
  'Typed read model of the active organized catalog. Derived from facts.records imports; never edit directly.';

CREATE TABLE research.refresh_state (
  id text PRIMARY KEY CHECK (id='current'),
  receipt_id text NOT NULL REFERENCES facts.import_receipts(id),
  model_version text NOT NULL,
  counts jsonb NOT NULL,
  refreshed_at timestamptz NOT NULL
);

CREATE TABLE research.topics (
  id text PRIMARY KEY,
  title text,
  payload jsonb NOT NULL
);

CREATE TABLE research.sources (
  id text PRIMARY KEY,
  url text NOT NULL,
  host text,
  title text,
  published text,
  published_date date,
  created_at timestamptz
);
CREATE INDEX sources_host_idx ON research.sources(host);

CREATE TABLE research.topic_sources (
  topic text NOT NULL,
  source_id text NOT NULL REFERENCES research.sources(id),
  family text NOT NULL,
  search_id text,
  PRIMARY KEY(topic, source_id, family)
);
CREATE INDEX topic_sources_source_idx ON research.topic_sources(source_id);

CREATE TABLE research.captures (
  id text PRIMARY KEY,
  source_id text,
  level text NOT NULL,
  state text NOT NULL,
  http_status integer,
  acquired_at timestamptz,
  artifact text NOT NULL,
  text_artifact text,
  metadata jsonb NOT NULL
);
CREATE INDEX captures_source_idx ON research.captures(source_id);

CREATE TABLE research.entities (
  id text PRIMARY KEY,
  name text NOT NULL,
  entity_type text NOT NULL,
  aliases text[] NOT NULL,
  payload jsonb NOT NULL
);
CREATE INDEX entities_name_trgm_idx ON research.entities USING gin(name gin_trgm_ops);

CREATE TABLE research.entity_aliases (
  entity_id text NOT NULL REFERENCES research.entities(id),
  alias text NOT NULL,
  entity_type text NOT NULL,
  PRIMARY KEY(entity_id, alias)
);
CREATE INDEX entity_aliases_trgm_idx ON research.entity_aliases USING gin(alias gin_trgm_ops);

-- Active observations and source references (one current version per id).
CREATE TABLE research.observations (
  id text PRIMARY KEY,
  version text NOT NULL,
  topic text NOT NULL,
  record_type text NOT NULL,
  content_role text NOT NULL,
  kind text NOT NULL,
  subtype text NOT NULL,
  title text NOT NULL,
  detail text NOT NULL,
  original_text text NOT NULL,
  event_start date,
  event_end date,
  temporal_context text,
  categories text[] NOT NULL,
  original_tags text[] NOT NULL,
  warnings text[] NOT NULL,
  input_ids text[] NOT NULL,
  method text,
  payload jsonb NOT NULL,
  search tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(detail, '') || ' ' || coalesce(original_text, '')), 'B')
  ) STORED
);
CREATE INDEX observations_topic_kind_idx ON research.observations(topic, record_type, kind);
CREATE INDEX observations_topic_role_idx ON research.observations(topic, content_role);
CREATE INDEX observations_event_idx ON research.observations(event_start, event_end);
CREATE INDEX observations_categories_idx ON research.observations USING gin(categories);
CREATE INDEX observations_search_idx ON research.observations USING gin(search);

CREATE TABLE research.observation_dates (
  observation_id text NOT NULL REFERENCES research.observations(id),
  ordinal integer NOT NULL,
  role text NOT NULL,
  original_expression text NOT NULL,
  start_text text,
  end_text text,
  start_date date,
  end_date date,
  precision text NOT NULL,
  calendar text NOT NULL,
  PRIMARY KEY(observation_id, ordinal)
);
CREATE INDEX observation_dates_range_idx ON research.observation_dates(start_date, end_date);

-- Values are exact decimals parsed from the observation's canonical value text;
-- the original expression, unit, scale and basis are retained beside them.
CREATE TABLE research.measurements (
  observation_id text NOT NULL REFERENCES research.observations(id),
  ordinal integer NOT NULL,
  topic text NOT NULL,
  metric text NOT NULL,
  value numeric,
  low numeric,
  high numeric,
  value_text text,
  comparator text NOT NULL,
  unit text,
  original_unit text,
  scale text,
  period text,
  basis text,
  original_basis text,
  value_kind text NOT NULL,
  qualifier text,
  original_expression text NOT NULL,
  PRIMARY KEY(observation_id, ordinal)
);
CREATE INDEX measurements_topic_unit_idx ON research.measurements(topic, unit, basis);
CREATE INDEX measurements_value_idx ON research.measurements(value);

CREATE TABLE research.observation_entities (
  observation_id text NOT NULL REFERENCES research.observations(id),
  ordinal integer NOT NULL,
  entity_id text,
  name text NOT NULL,
  canonical_name text,
  entity_type text NOT NULL,
  role text NOT NULL,
  PRIMARY KEY(observation_id, ordinal)
);
CREATE INDEX observation_entities_entity_idx ON research.observation_entities(entity_id, role);

CREATE TABLE research.attributions (
  observation_id text NOT NULL REFERENCES research.observations(id),
  ordinal integer NOT NULL,
  name text NOT NULL,
  role text NOT NULL,
  PRIMARY KEY(observation_id, ordinal)
);

CREATE TABLE research.observation_sources (
  observation_id text NOT NULL REFERENCES research.observations(id),
  ordinal integer NOT NULL,
  source_id text,
  capture_id text,
  url text,
  locator jsonb NOT NULL,
  PRIMARY KEY(observation_id, ordinal)
);
CREATE INDEX observation_sources_source_idx ON research.observation_sources(source_id);
CREATE INDEX observation_sources_capture_idx ON research.observation_sources(capture_id);

CREATE TABLE research.groups (
  id text PRIMARY KEY,
  topic text NOT NULL,
  group_kind text NOT NULL,
  title text NOT NULL,
  category text,
  anchor_id text,
  observation_count integer NOT NULL,
  source_url_count integer,
  version text,
  payload jsonb NOT NULL
);
CREATE INDEX groups_topic_kind_idx ON research.groups(topic, group_kind);

-- Only current member versions of active observations are listed.
CREATE TABLE research.group_members (
  group_id text NOT NULL REFERENCES research.groups(id),
  observation_id text NOT NULL REFERENCES research.observations(id),
  relation text NOT NULL,
  PRIMARY KEY(group_id, observation_id, relation)
);
CREATE INDEX group_members_observation_idx ON research.group_members(observation_id);

CREATE TABLE research.datasets (
  id text PRIMARY KEY,
  topic text NOT NULL,
  name text,
  state text NOT NULL,
  version text NOT NULL,
  orientation text,
  row_counts jsonb NOT NULL,
  series_count integer,
  value_count integer,
  payload jsonb NOT NULL
);

CREATE TABLE research.series (
  id text PRIMARY KEY,
  version text NOT NULL,
  topic text NOT NULL,
  dataset_id text NOT NULL REFERENCES research.datasets(id),
  title text NOT NULL,
  detail text NOT NULL,
  subtype text NOT NULL,
  metric text NOT NULL,
  basis text,
  original_basis text,
  unit text,
  scale text,
  unit_evidence text,
  row_role text NOT NULL,
  dimensions jsonb NOT NULL,
  hierarchy text[] NOT NULL,
  categories text[] NOT NULL,
  warnings text[] NOT NULL,
  source_id text,
  capture_id text,
  locator jsonb,
  payload jsonb NOT NULL
);
CREATE INDEX series_topic_basis_idx ON research.series(topic, basis, metric);
CREATE INDEX series_dataset_idx ON research.series(dataset_id);
CREATE INDEX series_title_trgm_idx ON research.series USING gin(title gin_trgm_ops);

-- Original-unit source_value and canonical value (source_value x scale) are both kept.
CREATE TABLE research.series_values (
  series_id text NOT NULL REFERENCES research.series(id),
  period text NOT NULL,
  source_row text NOT NULL,
  source_column text NOT NULL,
  dataset_id text NOT NULL,
  period_start date,
  period_end date,
  calendar text,
  period_role text,
  value numeric,
  source_value numeric,
  unit text,
  scale text,
  basis text,
  value_kind text,
  original_expression text,
  warnings text[] NOT NULL,
  PRIMARY KEY(series_id, period, source_row, source_column)
);
CREATE INDEX series_values_basis_period_idx ON research.series_values(basis, period_start);
CREATE INDEX series_values_dataset_idx ON research.series_values(dataset_id);

CREATE VIEW research.observation_evidence AS
  SELECT o.id AS observation_id, o.topic, o.kind, o.content_role, o.title, o.detail,
         o.event_start, o.event_end, os.ordinal, os.url, os.locator,
         s.id AS source_id, s.title AS source_title, s.host, s.published,
         c.id AS capture_id, c.level AS capture_level, c.state AS capture_state,
         c.acquired_at, c.artifact AS capture_artifact, c.text_artifact AS capture_text_artifact
  FROM research.observations o
  JOIN research.observation_sources os ON os.observation_id = o.id
  LEFT JOIN research.sources s ON s.id = os.source_id
  LEFT JOIN research.captures c ON c.id = os.capture_id;

CREATE VIEW research.series_points AS
  SELECT s.topic, s.id AS series_id, s.title, s.metric, s.basis, s.row_role, s.dimensions,
         d.name AS dataset_name, v.period, v.period_start, v.period_end, v.calendar,
         v.value, v.source_value, v.unit, v.scale, v.value_kind, v.original_expression,
         v.source_row, v.source_column, s.source_id, s.capture_id
  FROM research.series_values v
  JOIN research.series s ON s.id = v.series_id
  JOIN research.datasets d ON d.id = s.dataset_id;

-- Collection backlog/status straight from the lossless mirror.
CREATE VIEW research.extraction_status AS
  SELECT topic, payload->>'state' AS state, count(*) AS jobs
  FROM facts.records WHERE kind = 'extraction_jobs' GROUP BY 1, 2;
"""

MIGRATIONS = (
    (1, "record_mirror", MIRROR_SCHEMA),
    (2, "research_read_model", RESEARCH_SCHEMA),
)

MIGRATION_TABLE = """
CREATE SCHEMA IF NOT EXISTS facts;
CREATE TABLE IF NOT EXISTS facts.schema_migrations (
  version integer PRIMARY KEY,
  name text NOT NULL,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def checksum(sql):
    return hashlib.sha256(sql.encode()).hexdigest()


def migrate(connection, lock):
    """Apply pending migrations inside the caller's transaction; return applied versions."""
    connection.execute('SELECT pg_advisory_xact_lock(%s,%s)', lock)
    connection.execute(MIGRATION_TABLE)
    applied = dict(connection.execute('SELECT version, checksum FROM facts.schema_migrations').fetchall())
    known = {version for version, _, _ in MIGRATIONS}
    if set(applied) - known:
        raise ValueError('Database has migrations newer than this code; update the collector first')
    newly = []
    for version, name, sql in MIGRATIONS:
        digest = checksum(sql)
        if version in applied:
            if applied[version] != digest:
                raise ValueError(f'Applied migration {version} ({name}) differs from this code')
            continue
        connection.execute(sql)
        connection.execute('INSERT INTO facts.schema_migrations (version,name,checksum) VALUES (%s,%s,%s)',
                           (version, name, digest))
        newly.append(version)
    return newly


def status(connection):
    """Read-only migration status; creates nothing."""
    exists = connection.execute("SELECT to_regclass('facts.schema_migrations') IS NOT NULL").fetchone()[0]
    applied = dict(connection.execute('SELECT version, checksum FROM facts.schema_migrations').fetchall()) if exists else {}
    return [{'version': v, 'name': n, 'applied': v in applied,
             'checksum_matches': applied.get(v) == checksum(s) if v in applied else None}
            for v, n, s in MIGRATIONS]
