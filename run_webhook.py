#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════
  bulkmessage WEBHOOK RECEIVER + CLOUDFLARE TUNNEL
═══════════════════════════════════════════════════════════════

Запускает webhook-сервер (FastAPI) + (опционально) Cloudflare tunnel
для приёма events от Wazzup24 (delivered, read, replied, status_update).

Использование:
  py run_webhook.py              # только webhook сервер (127.0.0.1:8000)
  py run_webhook.py --tunnel     # webhook + cloudflared quick tunnel

Для прода — нужен named tunnel + persistent URL (см. WAZZUP24_KNOWLEDGE_BASE.md).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bulkmessage import config


def check_uvicorn():
    """Проверить что uvicorn и fastapi установлены."""
    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        print("❌ Не хватает пакетов: uvicorn fastapi")
        print("Установи: pip install uvicorn fastapi")
        sys.exit(1)


def check_cloudflared():
    """Проверить наличие cloudflared в PATH."""
    try:
        subprocess.run(
            ["cloudflared", "--version"],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def run_webhook_server():
    """Запустить webhook сервер через uvicorn."""
    import uvicorn
    from bulkmessage.webhook_app import app

    host = config.WEBHOOK_HOST
    port = config.WEBHOOK_PORT
    path = config.WEBHOOK_PATH
    print(f"🚀 Webhook сервер: http://{host}:{port}")
    print(f"   Endpoints:")
    print(f"     POST {path}")
    print(f"     POST /webhook/wazzup")
    print(f"     GET  /health")
    print()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )


def run_tunnel_in_background(local_port: int):
    """Запустить cloudflared quick tunnel в фоне."""
    print(f"🌐 Запускаю Cloudflare tunnel → http://127.0.0.1:{local_port}")
    print(f"   Подожди 5-10 сек, потом в логе появится trycloudflare URL")
    print()
    cmd = [
        "cloudflared",
        "tunnel",
        "--url", f"http://127.0.0.1:{local_port}",
        "--no-autoupdate",
    ]
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in process.stdout:
            print(f"[cloudflared] {line.rstrip()}")
            if "trycloudflare.com" in line:
                print()
                print("=" * 70)
                print("🔗 ТУННЕЛЬ ГОТОВ — скопируй URL выше и подпиши Wazzup:")
                print(f"   py setup_webhook.py --url {line.strip()}")
                print("=" * 70)
                print()
        process.wait()
    except FileNotFoundError:
        print("❌ cloudflared не найден в PATH")
        print("   Скачай: https://github.com/cloudflare/cloudflared/releases")
        print("   Или установи: winget install Cloudflare.cloudflared")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Запуск webhook сервера (+ опционально Cloudflare tunnel)")
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="Запустить Cloudflare quick tunnel параллельно с webhook сервером",
    )
    args = parser.parse_args()

    check_uvicorn()
    if args.tunnel and not check_cloudflared():
        print("❌ cloudflared не установлен")
        print("   Скачай: https://github.com/cloudflare/cloudflared/releases/latest")
        print("   Windows: добавь cloudflared.exe в PATH")
        sys.exit(1)

    if args.tunnel:
        import threading
        import time
        port = config.WEBHOOK_PORT
        tunnel_thread = threading.Thread(
            target=run_tunnel_in_background,
            args=(port,),
            daemon=True,
        )
        tunnel_thread.start()
        time.sleep(5)
        run_webhook_server()
    else:
        run_webhook_server()


if __name__ == "__main__":
    main()