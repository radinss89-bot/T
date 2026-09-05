import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL تنظیم نشده!")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# جدول بازار
cur.execute("""
CREATE TABLE IF NOT EXISTS stocks (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    price BIGINT NOT NULL DEFAULT 100,

    min_change INTEGER NOT NULL DEFAULT -10,
    max_change INTEGER NOT NULL DEFAULT 10,

    random_enabled BOOLEAN NOT NULL DEFAULT TRUE,

    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""")

# فقط یک بازار اولیه بساز
cur.execute("""
SELECT id FROM stocks LIMIT 1;
""")

stock = cur.fetchone()

if not stock:
    cur.execute("""
        INSERT INTO stocks
        (name, price, min_change, max_change, random_enabled)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        "BETA",
        100,
        -10,
        10,
        True
    ))

conn.commit()

cur.close()
conn.close()

print("================================")
print("✅ بازار ساخته شد")
print("================================")