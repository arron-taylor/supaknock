ALTER TABLE forms ADD COLUMN public_enabled INTEGER NOT NULL DEFAULT 0 CHECK(public_enabled IN (0,1));
CREATE TABLE form_public_limits (
 bucket TEXT NOT NULL,
 window_start INTEGER NOT NULL,
 count INTEGER NOT NULL,
 PRIMARY KEY(bucket,window_start)
);
