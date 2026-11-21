"""Set the panel admin password in a database (offline helper).

    python3 tools/set_password.py /var/lib/ec20-sip/ec20sip.db newpassword [username]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Db, hash_password  # noqa: E402


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path, password = sys.argv[1], sys.argv[2]
    username = sys.argv[3] if len(sys.argv) > 3 else None
    db = Db(path)
    auth = dict(db.get_setting("auth", {}) or {})
    auth["username"] = username or auth.get("username") or "admin"
    auth["password_hash"] = hash_password(password)
    db.set_setting("auth", auth)
    db.close()
    print("password updated for %s in %s" % (auth["username"], path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
