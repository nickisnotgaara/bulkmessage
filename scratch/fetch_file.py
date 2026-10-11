"""Скачать текстовый файл с сервера."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from remote import connect  # noqa: E402

REMOTE = sys.argv[1]
LOCAL = sys.argv[2]

c = connect()
try:
    sftp = c.open_sftp()
    sftp.get(REMOTE, LOCAL)
    sftp.close()
finally:
    c.close()
print(f"saved {LOCAL} ({os.path.getsize(LOCAL):,} bytes)")