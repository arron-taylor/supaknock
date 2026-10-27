# Web administration for AI

Native administrators manage shared provider credentials in **AI providers** (`/admin/ai/providers`), use **AI runtime** in the sidebar (`/admin/ai`) and **AI support** directly in a project's settings (`/admin/projects/{project}#ai-support`). Every operation uses the existing cookie and CSRF-protected `/api/v1` API. Customer sessions and project credentials do not grant administration access.


AI support stays in project settings, with nested **Settings**, **Models & keys**, **Allowed topics**, **Knowledge**, **Personality**, and **Preview** tabs. The former `/admin/projects/{project}/ai#sections` links normalize to the Drafts view under `#ai-support/knowledge/drafts`. Both tab levels support keyboard navigation and browser Back.

Account **AI providers** lists answer and decision credentials together, with provider filters, search and 25-row pages. Add credentials once with an explicit provider and allowed roles. Cloudflare has dedicated Account ID and Workers AI token fields and distinguishes R2 credentials. Editing a shared key lists affected projects; pausing, replacing, deleting or removing a role fences their unfinished work. Expanding a role never binds a project automatically. No secret, prefix or suffix is returned. Existing keys and project selections migrate without merging duplicates.

Project **Models & keys** groups answer and topic-decision settings with links to the account library. **Answer setup** keeps provider, explicit account-key selection and exact model in one draft. Models are available before any project save; **Load / refresh available models** reads the selected account keys when their catalogues need refreshing. **Save & verify** saves the choices and checks every selected key in place. Progress, failures and the link back to AI permissions stay in the same card. A failed or interrupted operation may have saved some choices; reload and review the saved setup before retrying. Provider, model or key changes turn AI off, and verification never enables it. **Disconnect this project** removes only the project bindings and selected model, preserving account credentials. Topic-decision settings retain their separate provider, key and model saves; their checkbox search preserves selections while filtering. Each project chooses its own models; verifying one model preserves other exact-model evidence, and text-only rechecks preserve existing tool evidence. Changing providers detaches this project's keys and turns its AI/gate off; account credentials and other projects stay intact. Removing unavailable decision credentials never silently disables an enabled gate: it fails closed. Credentials, tools and topic restrictions are never enabled implicitly.

**Settings** holds permissions, feedback destinations and usage limits. **Allowed topics** holds scope, examples, refusal messages and decision tests; its provider/key link opens Models & keys. Returning to a clean connection or scope view refreshes saved settings, while unsaved drafts stay intact. Changes in another session still produce explicit version conflicts. Actions have consistent gaps from adjacent controls and content, and completed history views remove their loading placeholder.

## Installation

The runtime page shows the pinned model, its disk footprint, current state, download progress, safe failure code, and last update. Download, cancellation, retry, and removal are explicit actions. The download token is a password input, cleared after submission and never stored in browser storage. The page links to the model's license and explains that files are downloaded to the server's persistent volume, not the browser. Removal opens a focused inline impact confirmation with Cancel, Escape support, and a final Remove model action; it warns that it disables AI across projects while retaining original knowledge. Fixed local worker bounds are explained; this release does not offer arbitrary model or resource configuration.

Reading this page starts no model acquisition or provider request. Pending progress is refreshed locally every three seconds, pauses while the browser tab is hidden, and stops once work finishes or its view is removed. Status updates retain focused controls where possible.

Each installation owner supplies their own Hugging Face token. No shared developer token is bundled, configured by default, or used as a fallback:

1. Sign in to your own Hugging Face account and accept the license agreement on the [EmbeddingGemma model page](https://huggingface.co/google/embeddinggemma-300m).
2. Create an [access token](https://huggingface.co/settings/tokens) in that same account with read access to this model.
3. Paste it into **Your Hugging Face access token** in **AI runtime**, then select **Download model to this server**.

The token is encrypted for the pending download and removed when it completes, fails, or is cancelled. Installed embeddings run locally without the token. If access is denied, the page directs the owner back to the account/license/token steps. Local `creds.txt` files are verification inputs only and are excluded from Git and image builds; the running application never reads them.

## Project setup

- **Models & keys** separates provider/model selection from account credentials and opt-in permissions. Project knowledge and recent context go to its chosen answer provider while embeddings stay local. Models come from explicitly refreshed catalogues; checks may consume provider usage.
- The shared library accepts up to **99 keys per provider**, named with exact account groups. Keys from the same provider account share quotas across projects; Cloudflare groups are derived from the encrypted account ID. Status and affected projects appear in the account library; exact-model readiness appears in project settings. Use explicit reload buttons to inspect current state without overwriting drafts.
- Expired cooldowns recover through one fenced attempt. Account retry clears cooldowns without provider I/O or capability grants. Invalid/billing-blocked keys still need repair. Rotation is fair across projects and tool rounds keep their original key. Revocation cancels bound project work; losing the last generation-ready key disables AI while enabled topic gates fail closed.
- AI, all five tools, and guest reports have independent switches. Fresh defaults come from the server and remain off. The UI explains grounded answers with optional additional search, direct human support, visitor confirmation, guest tracking limits, and cancellation after settings changes.
- Submission boards and website feedback-page links have separate fields. Saved previews use the server's resolver: flow-specific URL, then project URL, then the native destination board. Opening a link is a normal user action; displaying/saving one does not fetch it or add private parameters.
- Daily limits, response tokens, and tool rounds are editable. Today's UTC reserved allowances appear separately from provider-reported tokens. The UI does not estimate monetary cost or promise refunds for uncertain requests.

## Knowledge, drafts, and previews

**Knowledge** contains three focused views:

- **Library** shows searchable website pages and articles, including readiness, a focused reader and entry points to write an article or connect a website.
- **Sources** manages website/Markdown originals, source approval and activity, indexing, per-page crawl results and refreshes. Advanced crawl settings and history are secondary views. Reaching the configured page cap is normal bounded completion; actual fetch/robots/parsing errors remain failures. A failed refresh retains the last usable index.
- **Drafts** shows generated sections/FAQs for review and editing, supporting references and optional explicit approval into the library. Generation history remains available without crowding the main workspace.

Markdown file loading fills the editor; submitting performs the import. New sources require explicit approval. Refresh, cancellation, deactivation, unapproval and removal use current versions; conflicts stay visible. Unsaved edits require confirmation before leaving. Originals, generated text and source excerpts are rendered as text, never trusted HTML. External links retain HTTP(S), no-opener and no-referrer protections.

Generation covers all selected eligible passages, or explicitly selected website pages, in recoverable batches. Progress shows selected/total passages and completed/total batches; saved drafts survive a later batch failure, and an eligible retry resumes unfinished work. Source/model/readiness problems have specific guidance; website AI can remain off during authoring. See [the authoring API and bounded coverage rules](ai-authoring-contract.md).

Draft editing does not change original evidence or embeddings. **Approve saved answer** is a separate native-admin action: it creates an independent approved Markdown article and queues local indexing, preserving immutable provenance. Website refreshes never overwrite that article. Review for accuracy and duplicates before approval. Unrelated settings changes do not prevent editing completed drafts whose actual cited evidence is still eligible.
**Personality** loads or edits `soul.md`, with the server's character/byte limits and explicit save. Personality affects presentation, not evidence or authority. **Preview** runs the saved personality and retrieval pipeline with every assistant tool disabled. It shows durable results and original citations without creating a customer conversation or enabling AI.

Tabs support direct hash links, arrow keys, Home/End, and visible focus. Errors and asynchronous state have accessible announcements. Mobile layout stacks fields and keeps long URLs, source text, and citations within the viewport. All forms remain ordinary native controls.

## Verification boundary

The disposable preview in `scripts/ai-ui-preview.py` uses real APIs, SQLite, and workers with simulated provider, embedding, download, and crawl boundaries. It never reads real credentials or contacts external services. Browser checks against that fixture verify UI behavior, not live provider entitlement or fresh model acquisition. Those remain separate release checks. The original settings/authoring UI checkpoint passed 83 tests. Current key-pool behavior is covered by real SQLite tests and browser fixtures recorded in Groq/key-pool [verification](verification.md). Run `node --test tests/test_ai_admin_ui.mjs` for UI checks, including source-version boundaries, catalogue-gated defaults, pool controls, operation locking and preservation of unsaved permission choices.
