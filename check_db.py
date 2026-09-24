"""Quick Neon connectivity check. Run:  python check_db.py"""
import os, re, sys, time
try:
    from dotenv import load_dotenv; load_dotenv()
    import psycopg2
except ImportError:
    sys.exit("Missing packages. Run: pip install -r requirements.txt")

url = os.environ.get("NEON_DATABASE_URL", "").strip().strip('"').strip("'")
if not url:
    sys.exit("NEON_DATABASE_URL not found in .env")
url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
host = re.search(r"@([^/:]+)", url).group(1)
print(f"Host   : {host}")
print(f"Pooled : {'YES' if '-pooler' in host else 'NO (direct)'}")

t = time.time()
try:
    conn = psycopg2.connect(url, connect_timeout=15)
except Exception as e:
    sys.exit(f"❌ Connection FAILED: {e}")
cur = conn.cursor()
cur.execute("select current_database(), current_user, version()")
db, user, ver = cur.fetchone()
print(f"✅ Connected in {time.time()-t:.1f}s  db={db} user={user}")
print(f"   {ver.split(',')[0]}")
cur.execute("""select table_name from information_schema.tables
               where table_schema='public' order by 1""")
tables = [r[0] for r in cur.fetchall()]
print(f"Tables : {', '.join(tables) or '(none yet — server.py creates them on first start)'}")
for t_ in ("employees", "leave_balances", "leave_applications"):
    if t_ in tables:
        cur.execute(f"select count(*) from {t_}")
        print(f"   {t_:<20} {cur.fetchone()[0]} rows")
conn.close()
