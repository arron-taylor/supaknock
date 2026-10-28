-- Legacy decision checks have no individual usage-report marker. Preserve
-- those days conservatively rather than guessing which checks were billed.

-- Reconcile already reported calls; retain every uncertain reservation.
-- Provider totals are stored once per call by the serialized report operation.
UPDATE ai_usage SET reserved_tokens = reported_input_tokens + reported_output_tokens +
 COALESCE((SELECT SUM(r.tokens) FROM ai_usage_reservations r
           WHERE r.project_id=ai_usage.project_id AND r.day=ai_usage.day AND r.reported=0),0)
 WHERE NOT EXISTS (SELECT 1 FROM ai_decision_checks d
                   WHERE d.project_id=ai_usage.project_id AND d.day=ai_usage.day);
