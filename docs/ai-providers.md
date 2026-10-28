# AI provider configuration

AI chat, assistant tools and guest reports are independently off by default.
Configure encrypted credentials in the native administrator's AI provider
library, explicitly bind them to projects and roles, choose the exact model,
and verify its capabilities before enabling AI for a project.

Supported chat adapters include OpenAI, Anthropic Claude, OpenRouter, Ramp
Router, KodeKey, direct Google Gemini, Mistral, Groq and custom OpenAI-compatible
endpoints. Catalogue membership is not capability verification. Topic decisions
are configured separately. Provider changes invalidate incompatible selections
and work; there is no automatic provider fallback.

Ramp Router requests disable automatic Flex routing so interactive answers do
not enter the discounted, less predictable Flex queue. This keeps the selected
model, its reasoning settings and the independent evidence check unchanged.
Standard processing can cost more than Flex; SupaKnock does not request a premium
priority tier. A service tier explicitly pinned in a Router model ID remains
the administrator's choice. See [Router service-tier routing](https://docs.router.com/strategies/cost-efficient-routing).

Custom endpoints use public HTTPS, validated pinned DNS and no redirects by
default. Private or HTTP endpoints require `ALLOW_INTERNAL_AI_ENDPOINTS=true`;
metadata endpoints remain blocked. Keep provider keys server-side.

Local EmbeddingGemma is the default knowledge embedding option. A native
administrator may configure and synthetically verify an external compatible
embedding endpoint installation-wide. External indexing sends only approved
sources; originals and project-scoped vectors remain in the installation
database. Changing an embedding profile fences old work and disables AI until
reindexing and explicit re-enabling. See [local runtime](ai-runtime.md).

Use [AI administration](ai-web-admin.md), the [provider contract](ai-provider-contract.md)
and the [key-pool contract](ai-key-pool-contract.md) for settings, authorization,
capability evidence and quota behavior. Synthetic checks do not establish answer
quality or production readiness for a particular workload.
