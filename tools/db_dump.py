import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "data/ec20sip.db"
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
print("rules          :", [dict(r) for r in conn.execute("SELECT id,name,enabled,match_type,pattern,channels FROM rules")])
print("forward_log    :", [dict(r) for r in conn.execute("SELECT * FROM forward_log")])
print("settings.webhook:", [r[0] for r in conn.execute("SELECT value FROM settings WHERE key='webhook'")])
print("settings.dmtp   :", [r[0] for r in conn.execute("SELECT value FROM settings WHERE key='smtp'")])
print("sms            :", [dict(r) for r in conn.execute("SELECT id,peer,text,forwarded FROM sms")])