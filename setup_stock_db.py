import os
import psycopg2


DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    conn = psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )

    cur = conn.cursor()

    # ==========================================
    # MARKET
    # ==========================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT 'BetaCoin',
            price NUMERIC(18, 2) NOT NULL DEFAULT 100,
            min_change NUMERIC(10, 4) NOT NULL DEFAULT -0.05,
            max_change NUMERIC(10, 4) NOT NULL DEFAULT 0.05,
            auto_change BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)

    # ==========================================
    # USER HOLDINGS
    # ==========================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_holdings (
            user_id BIGINT PRIMARY KEY,
            amount NUMERIC(18, 6) NOT NULL DEFAULT 0
        )
    """)

    # ==========================================
    # MARKET HISTORY
    # ==========================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_history (
            id SERIAL PRIMARY KEY,
            price NUMERIC(18, 2) NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
    """)

    # ==========================================
    # DEFAULT MARKET
    # ==========================================

    cur.execute("""
        INSERT INTO market (
            id,
            name,
            price,
            min_change,
            max_change,
            auto_change,
            updated_at
        )
        VALUES (
            1,
            'BetaCoin',
            100,
            -0.05,
            0.05,
            TRUE,
            0
        )
        ON CONFLICT (id) DO NOTHING
    """)

    conn.commit()

    cur.close()
    conn.close()

    print("Market database is ready.")


if __name__ == "__main__":
    main()