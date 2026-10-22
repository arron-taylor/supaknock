#!/usr/bin/env python3
"""Run PostgreSQL contracts against an isolated Docker server or a test DSN.

No database URL/password is printed or passed in command arguments. The default
Docker instance is loopback-only and removed on exit. An explicitly supplied URL
must belong to a test server with CREATEDB permission; conftest creates and drops
only per-test databases and never migrates the URL's referenced database.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTS = [
    'tests/test_postgres.py', 'tests/test_storage_portability.py',
    'tests/test_api.py', 'tests/test_native.py',
    'tests/test_webhooks.py', 'tests/test_chat.py', 'tests/test_chat_customers.py',
    'tests/test_notifications.py', 'tests/test_push.py', 'tests/test_devices.py',
    'tests/test_connected_api.py', 'tests/test_resource_archive.py',
    'tests/test_ai_settings_api.py', 'tests/test_ai_reports_api.py',
    'tests/test_business_hours.py', 'tests/test_ai_storage.py',
    'tests/test_ai_ingestion.py', 'tests/test_ai_retrieval_selection.py',
    'tests/test_ai_job_races.py', 'tests/test_ai_key_pool.py',
    'tests/test_ai_account_keys.py', 'tests/test_ai_authoring.py',
    'tests/test_ai_knowledge.py', 'tests/test_ai_chat_isolation.py',
    'tests/test_ai_reports_storage.py', 'tests/test_ai_generation.py',
]
# These existing tests explicitly open SQLite files or inspect SQLite migration
# history. They remain covered in the normal suite. PostgreSQL migration,
# restart and lease behavior has independent real-server contracts above.
SQLITE_FILE_TESTS = (
    'test_network_retry_limit_restart_and_lease_fencing',
    'test_chat_backup_restart_and_atomic_push_failure',
    'test_customer_api_history_and_reads_survive_restart_and_backup',
    'test_shared_acknowledgements_survive_restart_and_supported_backup',
    'test_migration_backfills_outstanding_work_once_without_losing_existing_data',
    'test_push_backup_restore_and_url_defenses',
    'test_device_account_switch_and_restore', 'test_identity_and_backup',
    'test_restore_new_database_and_wrong_key_fail_closed',
    'test_archive_survives_restart_and_fixed_backup_restore',
    'test_contact_atomic_private_idempotent_clear_and_restore',
    'test_pre014_migration_preserves_encryption_evidence_and_job_authority',
    'test_pre016_upgrade_preserves_ids_both_roles_history_and_backups',
    'test_group_cooldown_persists_and_new_keys_inherit_it',
    'test_startup_checks_nondefault_pool_secrets_and_supported_backup',
    'test_checkpoint_survives_expired_worker_lease_and_rejects_duplicate_or_wrong_batch',
    'test_reviewed_draft_approval_is_explicit_idempotent_and_indexed',
    'test_original_survives_restart_backup_and_provenance_cannot_change',
    'test_committed_owner_rules_survive_flags_handoff_rejection_and_backup',
    'test_confirmed_guest_history_survives_credential_cleanup_without_recovery',
)
SQLITE_ONLY = ' and '.join('not ' + name for name in SQLITE_FILE_TESTS)


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Docker test server operation failed: ' + args[0])
    return result.stdout


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url-file', type=Path, help='Mode-0600 file containing a test PostgreSQL URL')
    parser.add_argument('--image', default='postgres:17-alpine', help='Disposable PostgreSQL Docker image')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    container = None
    try:
        if args.url_file:
            if args.url_file.stat().st_mode & 0o077:
                parser.error('--url-file must not be readable by group or others')
            url = args.url_file.read_text().strip()
        elif os.environ.get('TEST_DATABASE_URL'):
            url = os.environ['TEST_DATABASE_URL']
        else:
            container = 'supaknock-pg-contract-' + uuid4().hex[:12]
            password = secrets.token_urlsafe(32)
            with tempfile.TemporaryDirectory(prefix='supaknock-pg-contract-') as directory:
                env_file = Path(directory) / 'postgres.env'
                env_file.write_text('POSTGRES_PASSWORD=' + password + '\n')
                env_file.chmod(0o600)
                docker('run', '--detach', '--name', container, '--env-file', str(env_file),
                       '-p', '127.0.0.1::5432', args.image)
            deadline = time.monotonic() + 60
            while True:
                ready = subprocess.run(['docker', 'exec', container, 'pg_isready', '-U', 'postgres'],
                                       capture_output=True).returncode == 0
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('PostgreSQL test server did not become ready')
                time.sleep(.5)
            metadata = json.loads(docker('inspect', container))[0]
            port = metadata['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
            url = f'postgresql://postgres:{password}@127.0.0.1:{port}/postgres?sslmode=disable'
        if not url.startswith(('postgresql://', 'postgres://')):
            parser.error('A PostgreSQL test connection URL is required')
        env = dict(os.environ, TEST_DATABASE_URL=url)
        selected = args.pytest_args
        if selected[:1] == ['--']:
            selected = selected[1:]
        if not selected:
            selected = [*DEFAULT_TESTS, '-k', SQLITE_ONLY]
        print('Running real PostgreSQL contracts with one isolated database per test.', flush=True)
        return subprocess.run([sys.executable, '-m', 'pytest', '-q', *selected], cwd=ROOT, env=env).returncode
    finally:
        if container:
            subprocess.run(['docker', 'rm', '-f', '-v', container], capture_output=True)


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, RuntimeError):
        # Connection strings and subprocess environment must never be exposed.
        print('PostgreSQL test setup failed; check Docker availability and the test URL file.', file=sys.stderr)
        raise SystemExit(1)
