import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, redirect, request


CHANNEL_ID = os.environ.get("LINEPAY_CHANNEL_ID", "").strip()
CHANNEL_SECRET = os.environ.get("LINEPAY_CHANNEL_SECRET", "").strip()
API_BASE = os.environ.get("LINEPAY_API_BASE", "https://api-pay.line.me").rstrip("/")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://pay-api.apricostudio.shop").rstrip("/")
FRONTEND_URL = os.environ.get(
    "FRONTEND_URL", "https://ptgaminglife.github.io/Aistudiopage/enroll.html"
)
ALLOWED_ORIGIN = "https://ptgaminglife.github.io"
DB_PATH = Path(os.environ.get("LINEPAY_DB_PATH", "/var/lib/linepay-gateway/orders.db"))

PLANS = {
    "earlybird": {"amount": 8800, "label": "早鳥價"},
    "regular": {"amount": 24000, "label": "單價"},
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_requests_by_ip = defaultdict(deque)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                transaction_id TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )


def db_execute(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(sql, params)


def db_one(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchone()


def signature(uri, body, nonce):
    message = CHANNEL_SECRET + uri + body + nonce
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def linepay_post(uri, payload, timeout):
    if not CHANNEL_ID or not CHANNEL_SECRET:
        raise RuntimeError("LINE Pay credentials are not configured")

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    nonce = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-LINE-ChannelId": CHANNEL_ID,
        "X-LINE-Authorization-Nonce": nonce,
        "X-LINE-Authorization": signature(uri, body, nonce),
    }
    response = requests.post(API_BASE + uri, data=body.encode("utf-8"), headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def frontend_redirect(status):
    separator = "&" if "?" in FRONTEND_URL else "?"
    return redirect(FRONTEND_URL + separator + urlencode({"pay": status}), code=303)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",", 1)[0].strip() or request.remote_addr or "unknown"


def rate_limited(ip):
    now = time.time()
    window = _requests_by_ip[ip]
    while window and window[0] < now - 600:
        window.popleft()
    if len(window) >= 10:
        return True
    window.append(now)
    return False


def clean_text(value, max_length):
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin == ALLOWED_ORIGIN:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/health")
def health():
    return jsonify(ok=True, service="linepay-gateway")


@app.route("/api/payments/request", methods=["OPTIONS"])
def payment_options():
    return ("", 204)


@app.post("/api/payments/request")
def payment_request():
    if request.headers.get("Origin") not in (None, ALLOWED_ORIGIN):
        return jsonify(error="不允許的網站來源"), 403
    if rate_limited(client_ip()):
        return jsonify(error="嘗試次數過多，請稍後再試"), 429

    data = request.get_json(silent=True) or {}
    plan_key = clean_text(data.get("plan"), 20)
    plan = PLANS.get(plan_key)
    name = clean_text(data.get("name"), 80)
    phone = clean_text(data.get("phone"), 30)
    email = clean_text(data.get("email"), 160)
    if not plan or not name or not phone or "@" not in email:
        return jsonify(error="報名資料不完整"), 400

    order_id = f"AI{int(time.time())}{secrets.token_hex(4)}"
    now = int(time.time())
    db_execute(
        """INSERT INTO orders
           (order_id, name, phone, email, plan, amount, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)""",
        (order_id, name, phone, email, plan_key, plan["amount"], now, now),
    )

    payload = {
        "amount": plan["amount"],
        "currency": "TWD",
        "orderId": order_id,
        "packages": [
            {
                "id": order_id,
                "amount": plan["amount"],
                "name": "Aprico AI Studio",
                "products": [
                    {
                        "name": f"AI 變現課・{plan['label']}",
                        "quantity": 1,
                        "price": plan["amount"],
                    }
                ],
            }
        ],
        "redirectUrls": {
            "confirmUrl": f"{PUBLIC_BASE_URL}/api/payments/confirm",
            "cancelUrl": f"{PUBLIC_BASE_URL}/api/payments/cancel?orderId={order_id}",
        },
    }

    try:
        result = linepay_post("/v3/payments/request", payload, timeout=45)
    except Exception:
        app.logger.exception("LINE Pay payment request failed for order %s", order_id)
        db_execute(
            "UPDATE orders SET status='request_error', updated_at=? WHERE order_id=?",
            (int(time.time()), order_id),
        )
        return jsonify(error="付款服務暫時無法使用，請稍後再試"), 502

    if result.get("returnCode") != "0000" or not result.get("info"):
        code = str(result.get("returnCode", "unknown"))
        app.logger.warning("LINE Pay rejected order %s with code %s", order_id, code)
        db_execute(
            "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
            ("rejected_" + code, int(time.time()), order_id),
        )
        return jsonify(error=f"LINE Pay 無法建立付款（代碼 {code}）"), 502

    info = result["info"]
    transaction_id = str(info.get("transactionId", ""))
    payment_url = (info.get("paymentUrl") or {}).get("web")
    if not transaction_id or not payment_url:
        return jsonify(error="LINE Pay 回傳資料不完整"), 502

    db_execute(
        "UPDATE orders SET transaction_id=?, status='reserved', updated_at=? WHERE order_id=?",
        (transaction_id, int(time.time()), order_id),
    )
    return jsonify(paymentUrl=payment_url)


@app.get("/api/payments/confirm")
def payment_confirm():
    transaction_id = clean_text(request.args.get("transactionId"), 32)
    order_id = clean_text(request.args.get("orderId"), 80)
    if not transaction_id or not order_id:
        return frontend_redirect("failed")

    order = db_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not order or order["transaction_id"] != transaction_id:
        app.logger.warning("Invalid confirmation parameters for order %s", order_id)
        return frontend_redirect("failed")
    if order["status"] == "paid":
        return frontend_redirect("success")

    uri = f"/v3/payments/{transaction_id}/confirm"
    payload = {"amount": order["amount"], "currency": "TWD"}
    try:
        result = linepay_post(uri, payload, timeout=50)
    except Exception:
        app.logger.exception("LINE Pay confirmation failed for order %s", order_id)
        return frontend_redirect("failed")

    if result.get("returnCode") == "0000":
        db_execute(
            "UPDATE orders SET status='paid', updated_at=? WHERE order_id=?",
            (int(time.time()), order_id),
        )
        return frontend_redirect("success")

    code = str(result.get("returnCode", "unknown"))
    db_execute(
        "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
        ("confirm_error_" + code, int(time.time()), order_id),
    )
    return frontend_redirect("failed")


@app.get("/api/payments/cancel")
def payment_cancel():
    order_id = clean_text(request.args.get("orderId"), 80)
    if order_id:
        db_execute(
            "UPDATE orders SET status='canceled', updated_at=? WHERE order_id=? AND status!='paid'",
            (int(time.time()), order_id),
        )
    return frontend_redirect("canceled")


init_db()

