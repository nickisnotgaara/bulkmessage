import sqlite3, pathlib
db = pathlib.Path("data/crm.db").resolve()
con = sqlite3.connect("file:" + db.as_posix() + "?mode=ro", uri=True)
q = lambda s: con.execute(s).fetchone()[0]
print("ПРИНЯТО, но подтверждения нет (люди):", q("""
 SELECT COUNT(DISTINCT contact_id) FROM messages WHERE message_id IS NOT NULL
  AND message_id NOT LIKE 'dryrun%'
  AND delivered_at IS NULL AND COALESCE(delivery_state,'') NOT IN ('delivered','rejected')
  AND status NOT IN ('delivered','read','answered')"""))
print("ЯВНО ОТКЛОНЕНО (подтверждён НЕ доставлен, люди):", q("""
 SELECT COUNT(DISTINCT contact_id) FROM messages WHERE message_id IS NOT NULL
  AND message_id NOT LIKE 'dryrun%' AND delivery_state='rejected'"""))
print("ДОСТАВЛЕНО (люди):", q("""
 SELECT COUNT(DISTINCT contact_id) FROM messages WHERE delivered_at IS NOT NULL
  OR delivery_state='delivered' OR status IN ('delivered','read','answered')"""))
con.close()
