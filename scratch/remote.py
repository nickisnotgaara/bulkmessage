"""SSH-хелпер для bulkmessage: подключение по паролю, exec и scp."""
import os
import sys

import paramiko

HOST = "217.149.23.102"
USER = "root"
PASSWORD = os.environ.get("BM_SSH_PASSWORD", "")


def connect(timeout: int = 20) -> paramiko.SSHClient:
    if not PASSWORD:
        raise SystemExit("BM_SSH_PASSWORD is empty")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return c


def run(cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Возвращает (exit_code, stdout, stderr)."""
    c = connect()
    try:
        _in, out, err = c.exec_command(cmd, timeout=timeout)
        so = out.read().decode("utf-8", "replace")
        se = err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        return code, so, se
    finally:
        c.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hostname; uptime"
    code, so, se = run(cmd)
    sys.stdout.write(so)
    if se.strip():
        sys.stderr.write(se)
    sys.exit(code)