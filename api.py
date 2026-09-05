import os
import random
from datetime import datetime

import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# این مقدار را خودت تغییر بده
ADMIN_ID = 123456789


def get_db():
    return psycopg2.connect(DATABASE_URL)


# =====================================
# GET MARKET
# =====================================

@app.get("/market")
def market():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, price, min_change,
               max_change, random_enabled, updated_at
        FROM stocks
        LIMIT 1
    """)

    stock = cur.fetchone()

    cur.close()
    conn.close()

    if not stock:
        return jsonify({
            "ok": False,
            "error": "market_not_found"
        }), 404

    return jsonify({
        "ok": True,
        "stock": {
            "id": stock[0],
            "name": stock[1],
            "price": stock[2],
            "min_change": stock[3],
            "max_change": stock[4],
            "random_enabled": stock[5],
            "updated_at": stock[6].isoformat()
        }
    })


# =====================================
# GET WALLET
# =====================================

@app.get("/wallet/<int:user_id>")
def wallet(user_id):

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT coins, name
        FROM users
        WHERE user_id = %s
    """, (user_id,))

    user = cur.fetchone()

    cur.close()
    conn.close()

    if not user:
        return jsonify({
            "ok": False,
            "error": "user_not_found"
        }), 404

    return jsonify({
        "ok": True,
        "user_id": user_id,
        "name": user[1],
        "coins": user[0]
    })


# =====================================
# ADMIN CHECK
# =====================================

def is_admin(user_id):

    try:
        return int(user_id) == ADMIN_ID
    except:
        return False


# =====================================
# ADMIN SET PRICE
# =====================================

@app.post("/admin/price")
def admin_price():

    data = request.get_json() or {}

    user_id = data.get("user_id")
    price = data.get("price")

    if not is_admin(user_id):
        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 403

    try:
        price = int(price)
    except:
        return jsonify({
            "ok": False,
            "error": "invalid_price"
        }), 400

    if price < 1:
        return jsonify({
            "ok": False,
            "error": "price_must_be_positive"
        }), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE stocks
        SET price = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM stocks LIMIT 1
        )
    """, (price,))

    conn.commit()

    cur.close()
    conn.close()

    return jsonify({
        "ok": True,
        "price": price
    })


# =====================================
# ADMIN RANDOM SETTINGS
# =====================================

@app.post("/admin/random")
def admin_random():

    data = request.get_json() or {}

    user_id = data.get("user_id")

    if not is_admin(user_id):
        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 403

    enabled = bool(data.get("enabled", True))

    try:
        min_change = int(data.get("min_change", -10))
        max_change = int(data.get("max_change", 10))
    except:
        return jsonify({
            "ok": False,
            "error": "invalid_change"
        }), 400

    if min_change > max_change:
        return jsonify({
            "ok": False,
            "error": "min_greater_than_max"
        }), 400

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE stocks
        SET random_enabled = %s,
            min_change = %s,
            max_change = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM stocks LIMIT 1
        )
    """, (
        enabled,
        min_change,
        max_change
    ))

    conn.commit()

    cur.close()
    conn.close()

    return jsonify({
        "ok": True,
        "random_enabled": enabled,
        "min_change": min_change,
        "max_change": max_change
    })


# =====================================
# RANDOM PRICE UPDATE
# =====================================

@app.post("/market/random-update")
def random_update():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, price, min_change, max_change,
               random_enabled
        FROM stocks
        LIMIT 1
    """)

    stock = cur.fetchone()

    if not stock:
        cur.close()
        conn.close()

        return jsonify({
            "ok": False,
            "error": "market_not_found"
        }), 404

    stock_id = stock[0]
    old_price = stock[1]
    min_change = stock[2]
    max_change = stock[3]
    enabled = stock[4]

    if not enabled:
        cur.close()
        conn.close()

        return jsonify({
            "ok": True,
            "changed": False,
            "price": old_price
        })

    change_percent = random.uniform(
        min_change,
        max_change
    )

    new_price = int(
        old_price * (1 + change_percent / 100)
    )

    if new_price < 1:
        new_price = 1

    cur.execute("""
        UPDATE stocks
        SET price = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (
        new_price,
        stock_id
    ))

    conn.commit()

    cur.close()
    conn.close()

    return jsonify({
        "ok": True,
        "changed": True,
        "old_price": old_price,
        "new_price": new_price,
        "change_percent": round(change_percent, 2)
    })


# =====================================
# BUY
# =====================================

@app.post("/buy")
def buy():

    data = request.get_json() or {}

    try:
        user_id = int(data.get("user_id"))
        amount = int(data.get("amount"))
    except:
        return jsonify({
            "ok": False,
            "error": "invalid_data"
        }), 400

    if amount <= 0:
        return jsonify({
            "ok": False,
            "error": "invalid_amount"
        }), 400

    conn = get_db()

    try:

        cur = conn.cursor()

        # قفل کاربر برای جلوگیری از خرید همزمان
        cur.execute("""
            SELECT coins
            FROM users
            WHERE user_id = %s
            FOR UPDATE
        """, (user_id,))

        user = cur.fetchone()

        if not user:
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "user_not_found"
            }), 404

        coins = user[0]

        # قیمت فعلی
        cur.execute("""
            SELECT id, price
            FROM stocks
            LIMIT 1
        """)

        stock = cur.fetchone()

        if not stock:
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "market_not_found"
            }), 404

        stock_id = stock[0]
        price = stock[1]

        total = price * amount

        if coins < total:
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "not_enough_coins",
                "coins": coins,
                "required": total
            }), 400

        # کم کردن پول
        cur.execute("""
            UPDATE users
            SET coins = coins - %s
            WHERE user_id = %s
        """, (
            total,
            user_id
        ))

        # در نسخه ساده فعلاً تعداد سهم در users نگه نمی‌داریم
        # مرحله بعد portfolio اضافه می‌شود.

        conn.commit()

        return jsonify({
            "ok": True,
            "action": "buy",
            "amount": amount,
            "price": price,
            "total": total,
            "coins": coins - total
        })

    except Exception as e:

        conn.rollback()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

    finally:
        cur.close()
        conn.close()


# =====================================
# HEALTH CHECK
# =====================================

@app.get("/")
def home():

    return jsonify({
        "ok": True,
        "service": "Beta Stock API",
        "status": "online"
    })


# =====================================
# RUN
# =====================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )