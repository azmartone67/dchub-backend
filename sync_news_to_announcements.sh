#!/bin/bash
# Copy fresh news rows → announcements. Safe to run hourly; ON CONFLICT handles dedup.
psql "$DATABASE_URL" <<'SQL'
INSERT INTO announcements (id, title, summary, source_url, source, published_date, discovered_at, category, url)
SELECT
  'news_' || n.id::text,
  n.title,
  COALESCE(n.description, ''),
  n.url,
  n.source,
  COALESCE(n.published_date::text, n.created_at::text),
  n.created_at::text,
  COALESCE(n.category, 'industry'),
  n.url
FROM news n
WHERE n.created_at > NOW() - INTERVAL '48 hours'
ON CONFLICT (id) DO NOTHING;
SQL
