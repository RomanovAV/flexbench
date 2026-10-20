CREATE TABLE activities (
  id INTEGER PRIMARY KEY,
  owner_id TEXT NOT NULL,
  status TEXT NOT NULL,
  scheduled_at INTEGER NOT NULL,
  external_id TEXT
);
CREATE INDEX idx_activities_external_id ON activities(external_id);
