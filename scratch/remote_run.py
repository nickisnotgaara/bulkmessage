"""Upload + run a script on the server (avoids shell quoting hell)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from remote import connect  # noqa: E402

LOCAL = sys.argv[1]
REMOTE = sys.argv[2] if len(sys.argv) > 2 else "/tmp/_probe.py"

c = connect()
try:
    sftp = c.open_sftp()
    sftp.put(LOCAL, REMOTE)
    sftp.close()
    _in, out, err = c.exec_command(f"cd /opt/bulkmessage && python3 {REMOTE}", timeout=120)
    so = out.read().decode("utf-8", "replace")
    se = err.read().decode("utf-8", "replace")
    code = out.channel.recv_exit_status()
finally:
    c.close()

sys.stdout.write(so)
if se.strip():
    sys.stderr.write(se)
sys.exit(code)