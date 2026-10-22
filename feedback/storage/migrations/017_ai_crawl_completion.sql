ALTER TABLE ai_crawl_runs ADD COLUMN stop_reason TEXT
    CHECK (stop_reason IS NULL OR stop_reason = 'page_limit');
