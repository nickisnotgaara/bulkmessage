"""Скачивание crm.db с сервера в локальный файл + проверка целостности."""
import hashlib
import os
import sys

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from remote import HOST, USER, PASSWORD, connect  # noqa: E402

REMOTE = "/opt/bulkmessage/data/crm.db"
LOCAL = sys.argv[1] if len(sys.argv) > 1 else "scratch/crm_from_server.db"


def main() -> int:
    c = connect()
    try:
        sftp = c.open_sftp()
        st = sftp.stat(REMOTE)
        print(f"remote size = {st.st_size:,} bytes, mtime = {st.st_mtime}")
        os.makedirs(os.path.dirname(LOCAL) or ".", exist_ok=True)
        print(f"downloading -> {LOCAL}")
        sftp.get(REMOTE, LOCAL)
        sftp.close()
    finally:
        c.close()

    n = os.path.getsize(LOCAL)
    h = hashlib.sha256()
    with open(LOCAL, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    print(f"local size  = {n:,} bytes")
    print(f"local sha256= {h.hexdigest()}")
    print("SIZE MATCH" if n == st.st_size else "!!! SIZE MISMATCH !!!")
    return 0 if n == st.st_size else 1


if __name__ == "__main__":
    raise SystemExit(main())