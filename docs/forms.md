# Forms

Forms collect private early access requests, signups or short questionnaires for a project. They are separate from feedback boards. Create and edit definitions in the web workspace; review submissions in web or Swift. Only native administrators may read answers. Participants, project credentials and chat credentials cannot list or read them.

## Share a public form page

Open a form’s editor and enable **Accept submissions** and **Enable public form page**, then save. Copy its link from **Public form page** in the inbox. Anyone with `/forms/FORM_ID` can fill it out without an account. Only the name, description, project name and current questions are public. Answers stay administrator-only; there is no public form directory or submission lookup.

Public pages are off by default. Closing submissions, disabling the public page or archiving the project makes its page and public API unavailable immediately. Pages carry noindex/nofollow, no tracking or replay scripts, and no browser answer storage. A direct link is a discovery choice, not a secret; share only questions and descriptions suitable for the public.

The supplied page uses `GET /api/v1/public/forms/{form_id}` for the definition and a signed two-hour ticket, then `POST /api/v1/public/forms/{form_id}/submissions` with that ticket, `client_request_id`, `answers`, and an empty honeypot `website`. The POST requires the installation’s exact Origin. It returns only `{ "ok": true }`, never answers or a submission identifier. This separate intake does not accept or expose a project token and grants no account, chat or participation rights.

Retries reuse the same ticket, request ID and answers while the page is open. They do not save twice or revive deleted answers. Edits invalidate old tickets; reload the current questions before submitting again. Closing/disabling also blocks retries. New IDs or a fresh page can create another entry for the same email; this is not a verified waitlist or duplicate-email registry.

Basic spam controls include bounded bodies, field validation, a honeypot, existing per-connection mutation limits, plus durable limits of ten new public submissions per validated client IP per UTC hour (across forms) and 200 per form per UTC hour. Only a keyed hash of the address is held in the short-lived counters. Retries do not consume these durable limits. Counters expire and are bounded; the database rechecks availability and commits counters, entries, events and notifications together. These limits are basic abuse protection, not proof of a human visitor.

## Embed a form in your website

Forms use the same div-and-script pattern as chat. In the form editor, turn on **Accept submissions** and **Enable website embed**, add your site's exact origin (for example `https://example.com`, without a path or trailing slash), and save. Copy **Embed on your website** from the form inbox. Embeds are independent of the public page and start disabled; existing forms stay disabled after upgrade. Allow up to 20 HTTPS origins; localhost HTTP is accepted for development. An origin is a browser boundary, not proof of a human or a secret.

Place the container wherever the form belongs. Put appearance in a JSON script before the widget script; `data-config` names that JSON element's ID without `#`. No token, iframe, or backend is needed:

```html
<div id="early-access"></div>
<script type="application/json" id="form-appearance">
{
  "submitText": "Count me in",
  "successTitle": "You're on the list.",
  "successMessage": "Thanks! We'll be in touch when there's a build to try.",
  "theme": {
    "mode": "auto",
    "radius": 14,
    "light": { "accent": "#147d50", "onAccent": "#ffffff" },
    "dark": { "accent": "#3ecf8e", "onAccent": "#092c1c" }
  }
}
</script>
<script defer src="https://box.example.com/form-widget.js"
  data-server="https://box.example.com"
  data-form="YOUR_FORM_UUID"
  data-target="#early-access"
  data-config="form-appearance"></script>
```

The form sits inline and fills its container. Control its width and placement in your own page CSS. Shadow DOM separates its internal styles from the page. The font inherits from its container. The widget never renders supplied HTML.

Appearance options:

- `theme.mode`: `auto` (system setting), `light`, or `dark`, exactly like chat.
- `theme.light` and `theme.dark`: chat's `accent`, `onAccent`, `background`, `surface`, `text`, `muted`, and `border` colors, using `#RGB` or `#RRGGBB`. Choose readable contrast; invalid colors use defaults.
- `theme.radius`: corner radius in pixels, clamped to 0–32; default 14.
- `density`: `comfortable` (default) or `compact`.
- `showTitle`: set `false` if your page already supplies a heading.
- `submitText`, `successTitle`, `successMessage`: plain text, bounded to 80, 120, and 500 characters respectively. Success copy does not send email or grant early access; promise only follow-up you will actually do.

For your website's theme toggle, call `SupaKnockForms.get('#early-access').setTheme('dark')` after the widget has mounted. Switching themes preserves typed answers. Alternatively, load the script without automatic `data-*` configuration and call `SupaKnockForms.mount('#early-access', {server: 'https://box.example.com', form: 'YOUR_FORM_UUID', theme: {mode: 'light'}})`. Choose automatic or manual mounting for each container. Multiple containers are supported; use unique container/config IDs. `get()` returns its controller, and `destroy()` aborts pending work and removes the widget for client-side navigation. Removing a widget cannot undo a submission already saved by the server.

The widget fetches `/api/v1/embed/forms/{form_id}` with its browser Origin and receives a signed two-hour ticket bound to the form, origin and current definition. It posts to `/api/v1/embed/forms/{form_id}/submissions` without cookies or account/project credentials. Changing questions, closing the form, disabling the embed, removing its origin, or archiving the project fences outstanding work. The database rechecks availability atomically. Embed and public-page intake share the same spam limits, private inbox and notifications. Loading a form creates no submission or notification. Same-ticket/request-ID/answer retries do not save twice; drafts and retry state live only in page memory. Reloading, remounting or changing answers can create a new submission; email is not deduplicated or verified.

For a strict CSP, allow your SupaKnock origin in `script-src`, `style-src` (or `style-src-elem`) and `connect-src`. `form-widget.css` is an external stylesheet, so no inline stylesheet exemption is needed. Keep your own script policy for manual mounting or theme-toggle code. No frame or third-party cookie permissions are needed.

The widget includes no tracking, replay, local storage or answer callbacks. Unlike the separate hosted page, an embed shares your website's script environment. Your website's scripts and recording tools may access answers even inside Shadow DOM. Exclude or mask form fields in your analytics setup; Shadow DOM is style isolation, not a privacy boundary. Keep sensitive forms on the separate hosted page if you cannot control your site's scripts.

## Connect your own website backend

1. Open Forms → Create form. Select the project. The starting template has email, name and message; edit it as needed. Forms start closed. Turn on Accept submissions and save when ready.
2. In project settings, create a dedicated project token granting `forms:submit`. This capability is independent of `read`, `participate`, `audience:read` and `manage`.
3. Your website posts to **your backend**. That backend validates visitor access and handles spam/rate limits, then sends the saved field keys to the SupaKnock API. Never expose the token in browser code or a native app bundle. The separate opt-in hosted public page above is also available; this token endpoint remains backend-only.

```http
POST /api/v1/forms/FORM_ID/submissions
Authorization: Bearer YOUR_PROJECT_TOKEN
Content-Type: application/json

{"client_request_id":"a7431955-36a7-411a-8fda-c041b42cfc20","answers":{"email":"person@example.com","name":"Sam","message":"I’d like early access."}}
```

Generate the ID once for a logical submission. Reuse the same ID and answers on timeout or retry. The response is `201` with only `id` and `created_at`; no submitted values are returned. The same ID with different values is a `409` conflict. Retries survive a form edit/closure, token rotation within the same project, or deletion of the answers, and never revive deleted data. A revoked token always fails. A new ID creates another entry, including for the same email; email is unverified contact information, not a customer identity.

The endpoint rejects Origin and Sec-Fetch-Site headers. This is a server credential boundary, not a substitute for your own site's spam prevention. Your backend must not expose a generic token-forwarding proxy. Regular SupaKnock request and body limits also apply.

## Definition and answers

One saved definition, active immediately on save. There is no question history, draft or publishing workflow. The `version` field protects concurrent edits only; it is not a form-definition versioning feature. After a `409`, reload and review the current state before saving again.

A form contains 1–20 fields with unique keys matching `[a-z][a-z0-9_]{0,39}`. Types: `text` (500 characters), `textarea` (4,000), `email` (254), `choice` (one of 1–30 distinct options). Values are strings; unknown keys, missing required values, invalid emails/choices and oversized values fail. Answers are bounded to 32 KiB of JSON inside the overall request limit. A project can have up to 100 forms.

Answers retain their original keys and values when a form changes. Views use the current label when available and show the raw key for removed questions. Choose stable keys and update your website/backend when changing required fields or choices. Closed forms and archived projects reject new submissions. Existing entries remain readable to administrators.

## Read and review submissions

On the web, open **Forms**, choose a form, then open an entry. On iPhone, use the **Forms** tab on the server you are signed in to. Both show the same private inbox, including submissions from public links and your backend.

Use **New**, **Reviewed** or **Archived** to keep track of entries and save the status when you change it. These are review labels; changing one does not email the person or grant early access. Only installation administrators can see answers. Form creation and question editing are available in the web workspace.

For iPhone alerts, enable **Form submissions** in Settings and save your device notification preferences. The installation’s push service must be enabled and iOS notifications allowed. This preference starts off and is separate from chat and feedback alerts. See [iPhone Forms instructions](https://supaknock.com/iphone).

## Administration

- `GET /api/v1/admin/forms`: paginated list, optionally filtered by `project_id`.
- `POST /api/v1/admin/projects/{project}/forms`: create the current definition.
- `GET/PUT /api/v1/admin/forms/{form_id}`: read/save; PUT includes current `version`.
- `GET /api/v1/admin/forms/{form_id}/submissions`: paginated list, optional `state` (`new`, `reviewed`, `archived`).
- `GET /api/v1/admin/form-submissions/{submission_id}`: read answers.
- `PUT /api/v1/admin/form-submissions/{submission_id}/state`: `{ "version": 1, "state": "reviewed" }`. No-op transitions create no extra events.
- `DELETE /api/v1/admin/form-submissions/{submission_id}`: body `{ "version": 1 }`, removes answers and cancels pending pushes. A minimal receipt and answer fingerprint remain to prevent retry resurrection. Existing backups retain their own historical data until removed through your retention process.

Cookie administrator mutations use Origin and CSRF. Native administrator bearer sessions work on the same routes. Storage rechecks current sessions, token revocation, project scope and permissions inside serialized transactions. No administrative API is granted by a project token.

## Notifications and release

New entries, immutable metadata events, shared inbox alerts and applicable push jobs commit together. No field labels, answers, emails or submitter identities enter the notification/relay payload. Shared inbox read status is separate from the submission review state. Review/edit/delete events do not notify again. Form events are not project webhooks in this release.

`form_notifications` is a separate device opt-in, default false. Update the relay with migration `006_forms.sql` and its matching runtime before enabling form push in the matching Swift/application release. Its new type is `form.submission_created`, resource ID is the submission UUID; the phone opens the private submission after authenticating to the matching installation. Existing request/chat preferences remain independent.

SQLite migrations 023–025 and PostgreSQL migrations 003–005 preserve the existing notification/push data and add forms. PostgreSQL/Supabase use the same private installation schema and atomic contracts. One instance; no transfer between providers. Back up using the existing maintenance operation before deployment. No email follow-up, invitations, exports, attachments or native definition builder is included.

The separate static marketing site links directly to the owner’s hosted iPhone signup form. No website handler or project credential is required.
