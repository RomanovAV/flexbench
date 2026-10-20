-- Existing migration optimizes status but still scans/sorts within each owner.
CREATE INDEX IF NOT EXISTS idx_activities_status ON activities(status);
