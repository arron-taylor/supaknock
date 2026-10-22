# Provider key pool contract

Current credential ownership: [account-level AI library](ai-web-admin.md) supersedes original project-owned key descriptions. Projects explicitly bind account keys to roles and choose their own models; historical provider/evaluation evidence below is not a new live-test claim.
Provider credentials belong to the shared native-administrator account library,
with at most 99 non-deleted keys per provider. Migration preserves larger legacy
libraries; they must reduce their count before adding more. Each project explicitly
selects generation/decision keys and exact models. See [account keys](ai-web-admin.md).
All administration rechecks the native session and current library/key versions.
Project bindings additionally require the current project configuration/policy version.
Secrets are encrypted by the service and never appear in views, suffixes, logs or errors.
Provider changes detach project bindings and clear its model; they do not delete shared
keys or evidence. Legacy project key-writing routes are compatibility operations on
this same library and therefore affect all bound projects when editing/deleting keys.
Verification is keyed by credential ID, exact model, role and credential version.
Changing a project's model preserves evidence for other models. Role removal detaches
its bindings; expanding roles never selects the key for a project automatically.

Quota groups are installation-wide `(provider, exact label)` identities. Labels
are not normalized; the default is `Default account`. Different keys do not imply
different provider accounts. New keys inherit their group's existing cooldown.

## Administrator storage methods

* `ai_provider_keys(project, principal)` returns the bounded pool view.
* `ai_provider_key_secrets(project, principal, version)` returns private
  `{id, protected_key}` rows for service-side duplicate-key checks. Never expose
  this result over HTTP. The subsequent mutation rechecks project and library versions.
* `ai_provider_key_add(project, principal, version, label, quota_group, protected_key)`.
* `ai_provider_key_update(project, principal, version, key_id, key_version, *, label, quota_group, enabled, protected_key=UNCHANGED)`.
* `ai_provider_key_delete(project, principal, version, key_id, key_version)`.
* `ai_provider_key_retry(project, principal, version, key_id, key_version)`.

Mutations return the pool view. Retry fences in-flight work in every project bound
to that quota group, then clears its shared account cooldown without
network I/O; it never repairs invalid/payment-blocked keys or fabricates probe
evidence. The next ordinary generation may claim the single recovery trial.

Pool fields: `project_id`, `provider`, `version`, `credential_version`,
`maximum_keys` (99), `selected_model`, `catalogue` (union, maximum 1,000 IDs),
`verified_key_count`, `available_key_count`, `keys`, `groups`.

Safe key fields: `id`, `label`, `quota_group`, `quota_group_id`, `version`,
`enabled`, `is_default`, `status`, `reason`, `retry_at`, `has_key`,
`catalogue_count`, `model_available`, `capabilities`, `capabilities_model`,
`eligible`, `created_at`, `updated_at`. No per-key full catalogue or credential.
`eligible` and `verified_key_count` describe exact-model/required-tool evidence;
availability additionally considers pause, blocked credentials, leases and group
cooldown. Availability counts eligible key choices, not parallel capacity.

Statuses: `paused`, `blocked`, `unverified`, `cooling_down`, `recovering`, `busy`,
`ready`. Group rows contain `id`, `label`, `status`, `reason`, `retry_at`,
`health_epoch`. Reasons are null or fixed safe codes, never provider text.

`ai_provider_snapshot(project, principal, version, *, key_id=None)` keeps its
legacy fields and adds `key_id`, `key_version`, `health_epoch`, `group_epoch`,
`quota_group`, `catalogue`, `capabilities`. An omitted ID selects the default key.

`ai_store_provider_evidence(project, principal, version, credential_version,
model, *, catalogue=None, capabilities=None, key_id=None, key_version=None,
health_epoch=None, group_epoch=None)` preserves its legacy signature and settings
view return. New services pass every captured fence. A successful explicit
capability probe can repair an invalid/payment-blocked key; catalogue refresh
alone cannot. Evidence whose catalogue union exceeds 1,000 IDs is rejected.

`ai_provider_check_outcome(project, principal, version, key_id, key_version,
health_epoch, group_epoch, outcome, *, retry_after=None)` records a check failure
with those exact fences and returns the pool view.

## Worker methods

`ai_provider_claim(job_id, token, current_time=None)` atomically pins one key to
the current job attempt. Success returns `available: true` and private
`provider`, `model`, `key_id`, `key_version`, `protected_key`, `capabilities`,
`credential_version`, `quota_group_id`, `health_epoch`, `group_epoch`.
Unavailable returns `available: false`, `reason` (`cooldown`, `busy`,
`unverified`), and `retry_at`. Stale job authority is a conflict.

All tool rounds reuse that key. Dispatch rechecks the key, model, group and lease
and stamps the existing usage reservation. `ai_renew_job` also renews an owned
recovery trial. A reclaimed attempt chooses again; the old worker cannot act.

`ai_provider_outcome(job_id, token, call_index, outcome, *, retry_after=None,
current_time=None)` returns `{accepted, retryable, retry_at}`. It records health
only for that dispatched reservation; the worker owns retry/finish transitions.
`ai_provider_defer(job_id, token, retry_at, current_time=None, *, error="unavailable")` releases an unpaid
attempt without consuming its retry allowance. Paid attempts retain both attempt
count and conservative usage. Existing maximum attempts remain three. Total
waiting is bounded by original job creation plus 300 seconds; a later retry
fails safely and transfers a live chat to human support. Safe errors are
`unavailable`, `rate_limited`, and `timeout`.

Outcomes: `success`, `unchanged`, `capability_unverified`, `rate_limited`, `quota_exhausted`, `invalid_credentials`,
`payment_required`, `access_denied`, `model_or_endpoint_unavailable`, `transient`.
`unchanged` records a call without changing health; `capability_unverified`
clears only selected-model capability evidence after an explicit failed probe.
Rate limiting preserves capabilities and cools the whole account group, honoring
Retry-After and bounded exponential backoff. `quota_exhausted` cools it for 24
hours. Invalid credentials/payment remain blocked until explicit successful
probe or replacement. Access/model denial clears that key's selected-model
evidence. Transient failures cause a short group cooldown. Only a matching
recovery epoch may clear its cooldown; late success cannot erase newer failures.
Concurrent failures from the same unrecovered cycle may only extend its limit.
Manual retry and successful recovery advance a persisted reset epoch, preventing
older failures from recreating or extending a later cooldown cycle.

Migration 014 copies the existing encrypted key and exact verification into one
default pool entry without changing configuration versions or existing job
authority. Groups, bindings and cooldowns persist in SQLite and backups. Startup
secret validation includes every retained pool key. Migration performs no I/O
outside SQLite and never downloads or probes a provider.
