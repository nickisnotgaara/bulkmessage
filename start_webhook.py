#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════
  ОДИН ЗАПУСК: webhook + туннель + автоподписка Wazzup
═══════════════════════════════════════════════════════════════════════

Раньше для поднятия вебхука надо было сделать 4 шага руками:
  1. py run_webhook.py            — сервер
  2. py run_webhook.py --tunnel   — туннель (в ДРУГОМ окне)
  3. выдернуть URL из лога cloudflared глазами
  4. py setup_webhook.py --url <url>

Шаг 3-4 — главный источник ошибок: URL надо скопировать руками, и
.subscribe_wazzup_webhook() надо вызвать ДО того, как сервер поднялся
(Wazzup сразу шлёт test event — если сервер не слушает, подписка
молча не работает, и мы узнаём об этом только через сутки).

Этот скрипт делает всё сам и в правильном порядке:
  1. поднимает webhook-сервер (в фоне)
  2. поднимает туннель и ВЫТЯГИВАЕТ URL из его вывода
  3. подписывает Wazzup на events
  4. ПРОВЕРЯЕТ, что /health отвечает ЧЕРЕЗ туннель (не с localhost!)
  5. пишет URL в .env.local → sender знает, что вебхук есть
  6. держит всё живым; по Ctrl+C возвращает .env.local как было

Использование:
  py start_webhook.py                 # всё автоматом (рекомендую)
  py start_webhook.py --no-tunnel     # только локальный сервер
  py start_webhook.py --install       # докачать cloudflared в ./tools
  py start_webhook.py --check         # проверить текущую подписку и сервер

Почему важно: без работающего вебхука фазовый режим НЕ РАБОТАЕТ.
Фазы Telegram/MAX берут список провалов только из явных событий
'failed' от Wazzup. Без вебхука фаза 1 отдаст ~90 вместо ~112,
а фазы 2 и 3 будут пустыми.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Без этого при перенаправлении вывода (лог-файл, фоновый процесс) print()
# копит в буфере и вы ничего не видите, пока процесс не завершится. А здесь
# всё по делу печатается ПО МЕРЕ ГОТОВНОСТИ — туннель может подниматься
# полминуты, и молчание в этот момент выглядит как зависание.
try:
    sys.stdout.reconfigure(line_buffering=True)  # Python 3.7+
except Exception:
    pass

TOOLS_DIR = ROOT / "tools"
ENV_LOCAL = ROOT / ".env.local"
DATA_DIR = ROOT / "data"

# Подписки — те же, что в setup_webhook.py
SUBSCRIPTIONS = {
    "messagesAndStatuses": True,        # входящие + статусы доставки (delivered/failed)
    "contactsAndDealsCreation": False,  # не нужно
    "channelsUpdates": True,            # смена статуса канала → halt
    "wabaTemplatesStatus": True,        # модерация шаблонов WABA
}

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


# ─────────────────────────── cloudflared ───────────────────────────

def find_cloudflared() -> str | None:
    """Ищет cloudflared: сначала локальный ./tools, потом PATH."""
    local = {
        "cloudflared.exe": TOOLS_DIR / "cloudflared.exe",
        "cloudflared": TOOLS_DIR / "cloudflared",
    }.get(_exe_name())
    if local and local.exists():
        return str(local)
    return shutil.which("cloudflared")


def _exe_name() -> str:
    import os
    return "cloudflared.exe" if os.name == "nt" else "cloudflared"


def install_cloudflared() -> str:
    """Скачивает ОФИЦИАЛЬНЫЙ бинарь cloudflared в ./tools (локально в проект).

    Это НЕ системная установка: файл кладётся рядом с кодом и удаляется вместе
    с папкой tools. Ничего глобального не меняется.

    ВАЖНО про имена ассетов: Windows — это голый .exe (никакого zip!), Linux —
    тоже голый бинарь, только macOS упакован в .tgz. Проверено HEAD-запросами:
    zip-имени в релизе нет, и https://.../cloudflared-windows-amd64.exe.zip
    отдаёт 404.
    """
    import platform
    import shutil as _sh
    import tempfile
    import urllib.request

    TOOLS_DIR.mkdir(exist_ok=True)
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")

    if sys.platform == "win32":
        asset = "cloudflared-windows-arm64.exe" if arm else "cloudflared-windows-amd64.exe"
        target = TOOLS_DIR / "cloudflared.exe"
    elif sys.platform == "linux":
        asset = "cloudflared-linux-arm64" if arm else "cloudflared-linux-amd64"
        target = TOOLS_DIR / "cloudflared"
    elif sys.platform == "darwin":
        asset = "cloudflared-darwin-arm64.tgz" if arm else "cloudflared-darwin-amd64.tgz"
        target = TOOLS_DIR / "cloudflared"
    else:
        raise SystemExit(
            f"❌ Не знаю, какой бинарь качать для {sys.platform}/{machine}.\n"
            f"   Поставь cloudflared вручную и положи рядом: {TOOLS_DIR}"
        )

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    print(f"⬇️  Качаю {asset}")
    print(f"   {url}")

    tmp_path = None
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, \
                tempfile.NamedTemporaryFile(delete=False, dir=str(TOOLS_DIR)) as tmp:
            tmp_path = Path(tmp.name)
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                tmp.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r   {done/1_048_576:6.1f} / {total/1_048_576:.1f} MB ({pct}%)",
                          end="", flush=True)
        print()
    except Exception as e:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
        raise SystemExit(
            f"❌ Не скачалось: {e}\n"
            f"   Скачай вручную и положи сюда: {target}\n"
            f"   {url}"
        )

    if asset.endswith(".tgz"):
        import tarfile
        with tarfile.open(tmp_path) as t:
            for m in t.getmembers():
                if m.name.endswith("/cloudflared"):
                    with t.extractfile(m) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    break
    else:
        _sh.move(str(tmp_path), str(target))

    if tmp_path and tmp_path.exists():
        tmp_path.unlink()
    target.chmod(0o755)
    size_mb = target.stat().st_size / 1_048_576
    print(f"✅ Готово: {target} ({size_mb:.1f} MB)")
    return str(target)


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс (Windows и POSIX)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def clear_stale_url() -> None:
    """Убирает МЁРТВЫЙ webhook URL из .env.local.

    Зачем: quick-tunnel URL живёт ровно пока живёт процесс. Если прошлую
    сессию убили (жёсткий kill, крэш, закрыли окно), URL остаётся в
    .env.local — и preflight радостно рапортует «вебхук настроен», хотя все
    события уходят в пустоту. Именно так выглядело исходное состояние.

    Проверяем НЕ по PID: он переиспользуется, и на живом прогоне PID умершего
    процесса уже принадлежал другому — «процесс жив» врало, а URL был мёртв.
    Доверяем только САМОМУ URL: не отвечает = протух.

    atexit при «закрыли окно / жёсткий kill» может не сработать, поэтому
    чистка делается при СЛЕДУЮЩЕМ старте, а не при выходе.
    """
    try:
        from bulkmessage import config as _cfg
    except Exception:
        return
    url = _cfg.WAZZUP_WEBHOOK_URL
    if not url or not ENV_LOCAL.exists():
        (DATA_DIR / "webhook_runtime.json").unlink(missing_ok=True)
        return

    base = url.split("//", 1)[-1].split("/", 1)[0]
    try:
        with urllib.request.urlopen(f"https://{base}/health", timeout=12) as r:
            if r.status == 200:
                print(f"ℹ️  Вебхук {base} отвечает — оставляю как есть.")
                return
    except Exception:
        pass

    text = ENV_LOCAL.read_text(encoding="utf-8")
    for key in ("WAZZUP_WEBHOOK_URL", "BULK_PUBLIC_WEBHOOK_URL"):
        m = re.search(rf"(?m)^{key}=(.*)$", text)
        if not m:
            continue
        if key == "BULK_PUBLIC_WEBHOOK_URL" and m.group(1).strip() != url:
            continue
        text = text[:m.start()] + f"{key}=" + text[m.end():]
        ENV_LOCAL.write_text(text, encoding="utf-8")
    (DATA_DIR / "webhook_runtime.json").unlink(missing_ok=True)
    print("🧹 Убрал протухший WAZZUP_WEBHOOK_URL:")
    print(f"    {url}")
    print("    Мёртвый URL хуже, чем его отсутствие: preflight врал бы, что")
    print("    вебхук настроен, а события уходят в пустоту.")


def record_running_url(url: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "webhook_runtime.json").write_text(
        json.dumps({
            "pid": os.getpid(),
            "url": url,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def forget_running_url() -> None:
    marker = DATA_DIR / "webhook_runtime.json"
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == os.getpid():
            marker.unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────── .env.local ───────────────────────────

def read_env_local() -> dict[str, str]:
    if not ENV_LOCAL.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def write_env_value(key: str, value: str) -> str | None:
    """Записывает key=value в .env.local. Возвращает ПРЕДЫДУЩЕЕ значение
    (или None), чтобы восстановить его при выходе.

    Quick-tunnel URL живёт только пока живёт процесс. Если оставить его в
    .env.local после выхода, sender будет считать что вебхук есть, а события
    приходить перестанут — хуже, чем когда его нет вовсе.
    """
    if not ENV_LOCAL.exists():
        return None
    text = ENV_LOCAL.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
    old = m.group(1).strip() if m else None
    if m:
        text = text[:m.start()] + f"{key}={value}" + text[m.end():]
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    ENV_LOCAL.write_text(text, encoding="utf-8")
    return old


def restore_env_value(key: str, old: str | None) -> None:
    if not ENV_LOCAL.exists():
        return
    text = ENV_LOCAL.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
    if m:
        text = text[:m.start()] + (f"{key}={old}" if old else f"{key}=") + text[m.end():]
    ENV_LOCAL.write_text(text, encoding="utf-8")


# ─────────────────────────── туннель ───────────────────────────

def start_tunnel(binary: str, local_port: int, timeout: int = 60) -> tuple[str, subprocess.Popen]:
    """Поднимает quick tunnel и возвращает (base_url, process)."""
    print(f"🌐 Туннель → http://127.0.0.1:{local_port}")
    proc = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{local_port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    found: list[str] = []
    deadline = time.time() + timeout
    for line in proc.stdout:
        line = line.rstrip()
        if "ERR" in line and "Unable to reach" not in line:
            print(f"[cloudflared] {line}")
        m = TUNNEL_URL_RE.search(line)
        if m:
            found.append(m.group(0))
            print(f"[cloudflared] {line}")
            break
        if time.time() > deadline:
            break
    if not found:
        proc.terminate()
        raise SystemExit(
            f"❌ cloudflared не выдал trycloudflare URL за {timeout}с.\n"
            f"   Чаще всего это нет интернета или блокировка Cloudflare.\n"
            f"   Проверь: cloudflared --version"
        )
    return found[0], proc


def start_server(host: str, port: int) -> threading.Thread:
    """Поднимает uvicorn в фоне. Возвращает поток."""
    import uvicorn
    from bulkmessage.webhook_app import app

    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=True)
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    return th


def wait_local_health(port: int, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def check_tunnel_health(base_url: str, timeout: int = 150) -> bool:
    """Проверяет, что /health отвечает ЧЕРЕЗ туннель.

    Это важно: localhost-200 ничего не значит — Wazzup стучится снаружи.

    Таймаут именно 150с, а не 30: cloudflared печатает URL мгновенно, но его
    DNS обычно прокачивается 40–90 секунд. Первые попытки отдают
    «не удалось разрешить имя», и если ждать недолго — скрипт сообщил бы
    «не отвечает» на совершенно рабочем туннеле (проверено на живом прогоне:
    на 30-й секунде DNS не резолвился, на 75-й отвечал 200).
    """
    deadline = time.time() + timeout
    attempt = 0
    last_err = ""
    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=8) as r:
                if r.status == 200:
                    print(f"   (проверка №{attempt}, "
                          f"{int(timeout - (deadline - time.time()))}с потрачено)")
                    return True
        except Exception as e:
            last_err = type(e).__name__
        if attempt % 5 == 1:
            left = int(deadline - time.time())
            print(f"   ⏳ жду прокачку DNS (попытка {attempt}, осталось {left}с, "
                  f"последняя ошибка: {last_err})")
        time.sleep(5)
    return False


# ─────────────────────────── main ───────────────────────────

def do_check() -> int:
    from bulkmessage import config, wazzup
    import requests

    print("🔍 Проверка вебхука\n")
    url = config.WAZZUP_WEBHOOK_URL
    print(f"  WAZZUP_WEBHOOK_URL = {url or '(не задан)'}")

    if url:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        print(f"  🌐 DNS/доступность {host} … ", end="", flush=True)
        try:
            with urllib.request.urlopen(f"{url.rsplit('/', 1)[0]}/health", timeout=8) as r:
                print(f"{'OK' if r.status == 200 else r.status}")
        except Exception as e:
            print(f"НЕДОСТУПЕН ({type(e).__name__})")
            print("     → URL мёртв (quick tunnel умер). Перезапусти py start_webhook.py")

    try:
        r = requests.get(
            f"{config.WAZZUP_BASE_URL}{config.WAZZUP_WEBHOOKS_PATH}",
            headers=wazzup._headers(), timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "data" in data:
                data = data["data"]
            print(f"\n  Подписка в Wazzup:")
            print(f"    {data}")
        else:
            print(f"  ❌ GET /webhooks → HTTP {r.status_code}")
    except Exception as e:
        print(f"  ❌ не удалось прочитать подписку: {e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Один запуск: webhook + туннель + автоподписка Wazzup",
    )
    ap.add_argument("--no-tunnel", action="store_true",
                    help="только локальный сервер, без туннеля и подписки")
    ap.add_argument("--install", action="store_true",
                    help="скачать cloudflared в ./tools (локально, не в систему)")
    ap.add_argument("--check", action="store_true",
                    help="проверить текущую подписку и доступность URL")
    ap.add_argument("--no-subscribe", action="store_true",
                    help="поднять туннель, но не менять подписку Wazzup")
    ap.add_argument("--keep-url", action="store_true",
                    help="не откатывать URL в .env.local при выходе")
    args = ap.parse_args()

    if args.check:
        return do_check()

    if args.install:
        install_cloudflared()
        if args.no_tunnel:
            return 0

    from bulkmessage import config, wazzup

    # Чистим URL от МЁРТВОЙ прошлой сессии ДО любой работы.
    clear_stale_url()

    host = config.WEBHOOK_HOST
    port = config.WEBHOOK_PORT
    path = config.WEBHOOK_PATH

    if args.no_tunnel:
        print(f"🚀 Локальный webhook: http://{host}:{port}{path}")
        start_server(host, port)
        if not wait_local_health(port):
            raise SystemExit("❌ сервер не поднялся")
        print("   Без туннеля Wazzup до вас не достучится — фазы 2-3 будут пустыми.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    binary = find_cloudflared()
    if not binary:
        print("❌ cloudflared не найден.\n")
        print("   Один шаг — и он подтянется локально в ./tools (система не меняется):")
        print("     py start_webhook.py --install")
        print()
        print("   Или вручную: winget install Cloudflare.cloudflared")
        return 1

    print("=" * 66)
    print("  bulkmessage · вебхук + туннель + автоподписка")
    print("=" * 66)

    # 1) СЕРВЕР ПЕРВЫМ. Wazzup шлёт test event сразу после подписки —
    #    если сервер не слушает, подписка молча не работает.
    start_server(host, port)
    if not wait_local_health(port):
        raise SystemExit("❌ webhook-сервер не поднялся на порту " + str(port))
    print(f"✅ Сервер слушает http://{host}:{port}{path}")

    # 2) ТУННЕЛЬ + извлечение URL из его вывода
    base_url, proc = start_tunnel(binary, port)
    webhook_url = f"{base_url}{path}"
    print(f"✅ Туннель готов: {base_url}")

    # 3) ПРОВЕРКА ЧЕРЕЗ ТУННЕЛЬ, а не с localhost
    if not check_tunnel_health(base_url):
        print("⚠️  /health не отвечает через туннель — Wazzup не достучится.")
    else:
        print(f"✅ {base_url}/health отвечает СНАРУЖИ (Wazzup достучится)")

    # 4) ПОДПИСКА
    if args.no_subscribe:
        print("\n⏭️  Подписка не менялась (--no-subscribe)")
    else:
        ok, detail = wazzup.subscribe_wazzup_webhook(webhook_url, SUBSCRIPTIONS)
        if ok:
            print(f"✅ Wazzup подписан на {webhook_url}")
            print(f"   messagesAndStatuses / channelsUpdates / wabaTemplatesStatus")
        else:
            print(f"❌ Подписка не удалась: {detail}")
            print(f"   Повторить вручную: py setup_webhook.py --url {webhook_url}")

    # 5) ЗАПИСЬ В .env.local, чтобы sender/preflight знали про вебхук.
    #    URL пишем ВСЕГДА (иначе preflight ругается «webhook not set» и report
    #    предупреждает, что доставку подтвердить нечем). А --keep-url влияет
    #    только на откат: по умолчанию quick tunnel умирает вместе с процессом,
    #    и на выходе мы возвращаем .env.local как было.
    old_url = write_env_value("WAZZUP_WEBHOOK_URL", webhook_url)
    old_pub = write_env_value("BULK_PUBLIC_WEBHOOK_URL", webhook_url)
    write_env_value("BULK_PUBLIC_WEBHOOK_PATH", path)
    # Маркер с PID — на случай, если процесс убьют без finally.
    record_running_url(webhook_url)
    print(f"✅ WAZZUP_WEBHOOK_URL записан в .env.local")

    if args.keep_url:
        print("   --keep-url: URL останется в .env.local после остановки.")
    else:
        def _restore() -> None:
            restore_env_value("WAZZUP_WEBHOOK_URL", old_url)
            restore_env_value("BULK_PUBLIC_WEBHOOK_URL", old_pub)
            forget_running_url()
            print("\n↩️  URL в .env.local возвращён как был "
                  "(quick tunnel умирает вместе с процессом).")

        atexit.register(_restore)

    print()
    print("=" * 66)
    print(f"  ВЕБХУК ЖИВ: {webhook_url}")
    print("=" * 66)
    print("  Дальше (в другом окне):")
    print("    py main.py preflight    ← должна быть зелёная галочка вебхука")
    print("    py main.py sender")
    print()
    print("  Ctrl+C — остановить.  Рассылку я не запускаю.")
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
