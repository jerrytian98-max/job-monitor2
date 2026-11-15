"""Server-console-only role management for the cloud portal."""

import argparse

from cloud_portal import connect


def main():
    parser = argparse.ArgumentParser(description='Manage an existing cloud account role')
    parser.add_argument('action', choices=('promote', 'demote'))
    parser.add_argument('username', help='exact registered username')
    args = parser.parse_args()
    with connect() as conn:
        user = conn.execute('SELECT id,is_admin FROM users WHERE username=?', (args.username,)).fetchone()
        if user is None:
            parser.error('account not found')
        target = 1 if args.action == 'promote' else 0
        if args.action == 'demote' and user['is_admin']:
            total = conn.execute('SELECT COUNT(*) FROM users WHERE is_admin=1').fetchone()[0]
            if total <= 1:
                parser.error('cannot remove the last administrator')
        conn.execute('UPDATE users SET is_admin=? WHERE id=?', (target, user['id']))
    print(f'{args.action} complete for account id {user["id"]}')


if __name__ == '__main__':
    main()
