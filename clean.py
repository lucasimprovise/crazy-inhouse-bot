import sqlite3

conn = sqlite3.connect('inhouse.db')

rows = conn.execute("SELECT match_id, map, status FROM matches WHERE status='active'").fetchall()
print(f"Matchs bloqués trouvés : {len(rows)}")
for r in rows:
    print(f"  - {r[0]} | map: {r[1]}")

conn.execute("UPDATE matches SET status='cancelled' WHERE status='active'")
conn.commit()
conn.close()
print("✅ Nettoyé")
