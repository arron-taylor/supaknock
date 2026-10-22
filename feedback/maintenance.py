"""Local backup and account recovery only; no general-purpose management CLI."""
from pathlib import Path
import argparse
import getpass
import json
import os
from feedback.config import Config
from feedback.security import hasher
from feedback.storage import open_storage


def save_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(value)


def backup(config, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    provider = config.database_provider
    name = 'feedback.sqlite' if provider == 'sqlite' else 'feedback.dump'
    open_storage(config, initialize=False).backup(destination / name)
    os.chmod(destination / name, 0o600)
    save_private(destination / 'application.key', config.secret)
    save_private(destination / 'setup.token', config.setup_token)
    if provider != 'sqlite':
        save_private(destination / 'backup.json', json.dumps({'format_version': 1,
                     'database_provider': provider, 'database_file': name}, indent=2) + '\n')
    instructions = ('Restore these files together while the service is stopped. '
                    'See docs/operations.md. Keep the original database and volume for recovery.\n')
    if provider != 'sqlite':
        instructions += ('feedback.dump is a PostgreSQL custom-format archive. Restore it into a new empty '
                         'destination with pg_restore, then set APPLICATION_SECRET to the included '
                         'application.key value. This bundle contains no database connection password. '
                         'Do not copy the archive over a running database or treat it as SQLite.\n')
    else:
        instructions += 'Set APPLICATION_SECRET to this application.key value if it is supplied by environment.\n'
    save_private(destination / 'README.txt', instructions)


def main():
    parser = argparse.ArgumentParser(description='Local maintenance. Requires filesystem access to the installation.')
    sub = parser.add_subparsers(dest='operation', required=True)
    b = sub.add_parser('backup', help='Create a database backup and secret bundle in a new directory')
    b.add_argument('directory')
    r = sub.add_parser('recover-account', help='Set an existing native account password and revoke sessions')
    r.add_argument('email')
    args = parser.parse_args()
    cfg = Config.from_env()
    if cfg.database_provider == 'sqlite' and not cfg.database_path.is_file():
        parser.error('Database does not exist; check DATABASE_URL')
    if args.operation == 'backup':
        backup(cfg, args.directory)
        print('Backup bundle created. Store it privately.')
    else:
        password = getpass.getpass('New password (12–128 characters): ')
        confirmation = getpass.getpass('Repeat password: ')
        if password != confirmation or not 12 <= len(password) <= 128:
            parser.error('Passwords must match and contain 12–128 characters')
        open_storage(cfg, initialize=False).recover_account(args.email.strip().casefold(), hasher.hash(password))
        print('Password changed; existing sessions revoked.')


if __name__ == '__main__':
    main()
