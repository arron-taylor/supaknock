ALTER TABLE forms ADD COLUMN public_enabled BIGINT NOT NULL DEFAULT 0 CHECK(public_enabled IN (0,1));
CREATE TABLE form_public_limits (
 bucket TEXT NOT NULL,
 window_start BIGINT NOT NULL,
 count BIGINT NOT NULL,
 PRIMARY KEY(bucket,window_start)
);
