"""Explicit, offline move of the existing SupaKnock relay's hostname.

Not a schema migration or an automatic upgrade. The operator must stop the
installation, back it up, and verify both origins route to the same relay and
the existing credential authenticates there before calling this function.
No enrollment, credential rotation, job edits or other relay moves occur.
"""

OLD_ORIGIN = 'https://suggestion-box-relay.doomo.dev'
NEW_ORIGIN = 'https://relay.supaknock.com'


def move_supaknock_relay(storage):
    with storage.transaction(True) as connection:
        row = connection.execute('SELECT relay_url FROM push_settings WHERE singleton=1').fetchone()
        if row is None or row['relay_url'] == NEW_ORIGIN:
            return False
        if row['relay_url'] != OLD_ORIGIN:
            raise ValueError('This operation only moves the original SupaKnock relay')
        connection.execute('UPDATE push_settings SET relay_url=?,version=version+1 WHERE singleton=1 AND relay_url=?',
                           (NEW_ORIGIN, OLD_ORIGIN))
        return True
