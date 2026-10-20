NEXT = "SELECT id, scheduled_at FROM activities WHERE owner_id=? AND status='PLANNED' AND scheduled_at>=? ORDER BY scheduled_at, id LIMIT ?"
WINDOW = "SELECT id FROM activities WHERE owner_id=? AND status=? AND scheduled_at>=? AND scheduled_at<? ORDER BY scheduled_at, id LIMIT ?"
