# Business hours and manual follow-up

Business hours describe when your team normally answers human support. They do not report presence, guarantee a reply at opening, close conversations or disable messages. AI remains available according to its existing independent settings.

## Set up

Native administrators configure the shared schedule under **Your account → Account business hours** (`/account#business-hours`). This is one installation-wide account setting shared by all administrators, like the account AI credential library. It starts off; existing chats keep their previous behavior until explicitly enabled.

On iPhone, use **Settings → Account business hours** for the shared schedule, or **Projects → project → Business hours** for an override. All controls are native; see [native administration](https://supaknock.com/iphone). The chat inbox marks conversations with a follow-up email and the conversation’s **View contact** shortcut opens the private contact information.

Choose an IANA timezone, enable the notice, and enter weekly time ranges. Days without ranges are closed. Up to three non-overlapping ranges per day support breaks. Use 24-hour times; `24:00` denotes the end of a day. Split overnight shifts across the adjacent days. Timezone rules handle daylight-saving changes. There are no date-specific holiday exceptions in this version; adjust the weekly schedule when necessary. An enabled but empty week reports no scheduled live support rather than inventing an opening.

Under **Project settings → Website chat → Human support hours**, choose:

- **Use account hours** (default): subsequent account changes apply automatically.
- **Use project hours**: configure an independent weekly schedule and timezone.
- **Do not show hours for this project**: suppress the notice and new email collection even if account hours are enabled.

Saving hours does not enable website chat, AI, any AI tool or guest reports. Configuration saves are versioned; a concurrent edit returns a conflict and keeps the editor available to reload deliberately.

## Visitor experience

For direct human chat and AI-to-human handoff, the widget shows whether the conversation is within support hours, the next opening when outside them, and expandable weekly hours. Times are labeled in the schedule's timezone. Anyone may still send a message. The same conversation and normal message notifications are used; no separate ticket, account or queue is created. Returning to AI hides the human-hours card.

When **Offer an optional email for manual follow-up** is enabled and the widget is outside hours, visitors can add an email alongside their next message. It is optional. The message and address save in one transaction. Retrying the same message cannot duplicate it or restore an address that was subsequently removed. The API allows this opted-in contact on a human conversation even if business hours open between viewing the form and sending it. Active AI conversations reject contact-bearing messages until a human handoff occurs.

The address appears in the web team inbox/support panel and **Conversation info**, and in the updated iPhone app's **Conversation info → Follow-up email**. It is unverified contact information, not a login or identity claim. Use your email app to reply manually. Suggestion Box does not send email, check delivery or record those off-platform replies in chat.

Visitors without email are told to return to this browser for replies. Clearing their chat session removes their browser access; providing an email does not create an account or a way to recover that session. Only offer response promises your team can actually meet.

## Privacy and API

The separate email is available only to the authorized conversation owner and native administrators. It never enters the chat message body, AI context, public configuration, webhook events, push jobs or shared notification payloads. If a visitor types an email directly into their message, normal message handling—including their selected AI mode—still applies; the separation concerns the dedicated email field.

A visitor can remove their saved contact in **Chat options → Remove follow-up email**, including after hours/email collection are disabled. Removal clears the current address, not historical backups or copies already seen by an administrator. Installation backups include this private field with every supported database provider; protect them with the rest of your installation data. Saving hours off does not erase previously supplied contact details.

The generated [OpenAPI](openapi.json) is the exact HTTP contract:

- GET/PUT `/api/v1/admin/chat/business-hours`: shared settings.
- GET/PUT `/api/v1/admin/chat/projects/{project}/business-hours`: project mode and effective schedule.
- GET `/api/v1/chat/projects/{project}/config`: public `business_hours` availability for an allowed website origin.
- POST `/api/v1/chat/conversations/{id}/messages`: optional `follow_up_email`, with the normal stable `client_message_id`.
- POST `/api/v1/chat/conversations/{id}/contact/remove`: explicit current contact `version`.

Settings and mutations recheck current administrator/customer authority in the configured database. Contact-bearing sends recheck project settings, enabled email collection, conversation ownership/state and human mode atomically with message, event and delivery work. Existing session expiration, origin restrictions, rate limits and support-mode fences remain in force. WebSocket state updates include contact changes; direct WebSocket messages retain the existing body-only send contract.

The migration is additive. Keep the existing volume and application key. Use the existing fixed backup/restore maintenance operations. Feature implementation does not imply Docker Hub publication, production configuration or iPhone distribution; see [verification](verification.md) for the tested release state.
