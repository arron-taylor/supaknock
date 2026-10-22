# Application progress

## Developer introduction and first chat — October 21, 2026

Added a concrete product example, project explanation, integration diagram,
three starting paths, an expandable public screenshot gallery, and common
integration questions to the README. The exact local HTML walkthrough was
verified in a browser through a customer message and administrator reply.
Anonymous widget usage is distinguished from authenticated backend integration;
project secrets stay server-side. The iPhone gallery is labeled as an upcoming
companion preview. Runtime source and deployment configuration are unchanged.

## Docker README quickstart — October 21, 2026

The README identifies Docker Hub and the full published image address, embeds
the complete published-image Compose configuration, and walks through pulling,
startup health, reading `/data/setup.token`, first-administrator setup and later
sign-in. No application behavior or deployment configuration changed.

## Initial repository README — October 21, 2026

Prepared the standalone application for the private `arron-taylor/supaknock`
repository. The README now provides product navigation, a feature overview,
Docker setup, integration entry points, and clear links to supaknock.com.
Detailed environment settings live in `docs/configuration.md`. The iPhone
companion is described as coming soon. No runtime behavior changed.

Documentation checks and the initial source review are recorded in
[verification](docs/verification.md). Public visibility and production
rollout are separate from this private source upload.

## Independent application repository — October 21, 2026

Application source, migrations, tests, dependency locks, Docker files and current
public documentation now live together in this repository. Website and private
supporting work are separate. Runtime application code is unchanged by this move.
Local verification is recorded in [docs/verification.md](docs/verification.md).
No GitHub publication or production deployment is part of this reorganization.

Known limitation carried forward: optional podcast AI can return an invalid
answer format after successful tool reads. Repair remains separate work.
