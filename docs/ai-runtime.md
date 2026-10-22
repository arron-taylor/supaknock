# Local embedding runtime

The application image includes the pinned CPU inference libraries from
`requirements-ai.lock`. It does not include model weights or credentials.

Local EmbeddingGemma weights are acquired only after an explicit native-admin
action, using the installation owner's Hugging Face access token. Downloads
follow the pinned model manifest and remain on the persistent volume, outside
image layers. Preserve that volume across restarts and upgrades.

The ordinary source/test installation does not require the large inference
dependencies. For the complete packaged runtime, build the application Dockerfile.
Keep the app's exact dependency locks and model manifest together when releasing.

Model availability, successful acquisition, synthetic inference and answer
quality are separate checks. A cached model does not prove a fresh authenticated
download works. See [AI administration](ai-web-admin.md), [provider setup](ai-providers.md)
and [operations](operations.md) for activation, approved knowledge and backups.
