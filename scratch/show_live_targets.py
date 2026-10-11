"""Показать 10 контактов, КОТОРЫМ ПОЙДЁТ реальное сообщение.

Ничего не отправляет. Только отбор — тот же самый код, что и в боевом
прогоне, поэтому список совпадёт с реальным.

Правило AGENTS.md §1.0: перед боевой отправкой владелец должен назвать
номера. Посмотреть список заранее дешевле, чем объясняться потом.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bulkmessage import config, notify, sender, state  # noqa: E402

LIMIT = 10

cur = state.reset_daily_if_new_day(state.load_state())

candidates = sender._select_contacts_for_cycle()
targets = sender._collect_fresh_waba_targets(candidates, cur)[:LIMIT]

print("=" * 70)
print(f"КОНТАКТЫ ДЛЯ БОЕВОГО ТЕСТА ({len(targets)} из "
      f"{len(candidates)} кандидатов)")
print("=" * 70)
print(f"{'#':<4}{'телефон':<17}{'имя':<24}{'категория':<12}")
print("-" * 70)
for i, c in enumerate(targets, 1):
    print(f"{i:<4}{notify.pretty_phone(c.get('phone','')):<17}"
          f"{(c.get('name') or '—')[:22]:<24}"
          f"{str(c.get('category') or '—')[:10]:<12}")

print()
print("=" * 70)
print("ЧТО ВАЖНО ЗНАТЬ ПЕРЕД ЗАПУСКОМ")
print("=" * 70)
print(f"   Пул доступен: {config.DB_PATH}")
print(f"   DRY_RUN сейчас: {config.DRY_RUN}   ← боевой режим уже включён")
print()
print("   Обе точки вебхука мертвы:")
print("     • локальный 127.0.0.1:8000 — не отвечает")
print("     • .env.local указывает на mug-cycles-attempt-ranging.trycloudflare.com")
print("       — домен НЕ резолвится")
print("     • Wazzup подписан на interested-registrar-acceptance-jet.trycloudflare.com")
print()
print(f"   UNKNOWN_GRACE_MINUTES = {config.UNKNOWN_GRACE_MINUTES} мин")
print("   → принятые без подтверждения через час уйдут в ОТКАТ,")
print("     то есть в ЛИЧНЫЕ аккаунты Telegram/MAX.")
print("     Telegram сейчас троттлит (CHANNEL_TGAPI_FLOOD_WAIT).")
print()
print("   БЕЗОПАСНЫЙ ВАРИАНТ для теста:")
print(f"     BULK_UNKNOWN_GRACE_MINUTES=1440  → откат не сработает сегодня")
print( "     BULK_ALLOW_NO_DELIVERY_SIGNAL=1   → сторож не убьёт прогон")