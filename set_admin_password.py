"""Set or change the admin panel password (it is asked for twice and never shown).

    python set_admin_password.py

Changing it logs every admin out. The password is stored only as a salted hash in the database.
"""
import getpass
import sys

from app import db, security

db.init()
pw = getpass.getpass("New admin password (at least 10 characters): ")
if len(pw) < 10 or pw.lower() == pw or pw.upper() == pw or not any(ch.isdigit() for ch in pw):
    sys.exit("Too weak. Use 10 or more characters with upper case, lower case and a number.")
if pw != getpass.getpass("Type it again: "):
    sys.exit("The two passwords did not match.")
db.set_admin_pw_hash(security.hash_password(pw))
print("Done. Open http://localhost:8000/admin and log in.")

