CREATE TABLE IF NOT EXISTS countries (
    country_id text PRIMARY KEY,
    name_es text,
    admin_name text,
    continent text,
    subregion text
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    mode text NOT NULL,
    requested_at timestamptz NOT NULL,
    completed_at timestamptz,
    status text NOT NULL,
    record_count integer NOT NULL DEFAULT 0,
    http_status integer,
    response_ms integer,
    query jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text
);

CREATE INDEX IF NOT EXISTS ingestion_runs_source_completed_idx
    ON ingestion_runs (source, completed_at DESC);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    sha256 text PRIMARY KEY,
    source text NOT NULL,
    fetched_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    query jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS raw_snapshots_expiry_idx ON raw_snapshots (expires_at);

CREATE TABLE IF NOT EXISTS published_state (
    mode text NOT NULL,
    event_id text NOT NULL,
    updated_ms bigint NOT NULL,
    PRIMARY KEY (mode, event_id)
);

CREATE TABLE IF NOT EXISTS event_versions (
    mode text NOT NULL CHECK (mode IN ('live', 'replay', 'historical')),
    test_case boolean NOT NULL DEFAULT false,
    event_id text NOT NULL,
    updated_ms bigint NOT NULL,
    source text NOT NULL,
    ingested_at timestamptz NOT NULL,
    replayed_at timestamptz,
    event_time timestamptz NOT NULL,
    magnitude double precision,
    magnitude_type text,
    event_type text NOT NULL,
    place text,
    longitude double precision NOT NULL,
    latitude double precision NOT NULL,
    depth_km double precision,
    country_id text REFERENCES countries(country_id),
    original jsonb NOT NULL,
    PRIMARY KEY (mode, event_id, updated_ms)
);

CREATE INDEX IF NOT EXISTS event_versions_id_idx ON event_versions (mode, event_id, updated_ms DESC);
ALTER TABLE event_versions ADD COLUMN IF NOT EXISTS test_case boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS events_current (
    mode text NOT NULL CHECK (mode IN ('live', 'replay', 'historical')),
    test_case boolean NOT NULL DEFAULT false,
    event_id text NOT NULL,
    updated_ms bigint NOT NULL,
    source text NOT NULL,
    ingested_at timestamptz NOT NULL,
    replayed_at timestamptz,
    event_time timestamptz NOT NULL,
    magnitude double precision,
    magnitude_type text,
    event_type text NOT NULL,
    place text,
    longitude double precision NOT NULL,
    latitude double precision NOT NULL,
    depth_km double precision,
    country_id text REFERENCES countries(country_id),
    original jsonb NOT NULL,
    PRIMARY KEY (mode, event_id)
);

CREATE INDEX IF NOT EXISTS events_current_time_idx ON events_current (mode, event_time DESC);
CREATE INDEX IF NOT EXISTS events_current_country_time_idx ON events_current (mode, country_id, event_time DESC);
CREATE INDEX IF NOT EXISTS events_current_magnitude_idx ON events_current (mode, magnitude);
ALTER TABLE events_current ADD COLUMN IF NOT EXISTS test_case boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS excluded_events (
    mode text NOT NULL,
    event_id text NOT NULL,
    updated_ms bigint NOT NULL,
    reason text NOT NULL,
    source text NOT NULL,
    original jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mode, event_id, updated_ms)
);

CREATE TABLE IF NOT EXISTS quarantine (
    id bigserial PRIMARY KEY,
    mode text NOT NULL,
    event_id text,
    reason text NOT NULL,
    source text NOT NULL,
    original jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS quarantine_created_idx ON quarantine (created_at DESC);
