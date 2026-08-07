import base64
import hashlib
import hmac
import json
import os
import sqlite3
import uuid

import requests


channel_id = os.environ["LINEPAY_CHANNEL_ID"].strip()
channel_secret = os.environ["LINEPAY_CHANNEL_SECRET"].strip()
api_base = os.environ.get("LINEPAY_API_BASE", "https://api-pay.line.me").rstrip("/")
db_path = os.environ.get("LINEPAY_DB_PATH", "/var/lib/linepay-gateway/orders.db")

with sqlite3.connect(db_path) as conn:
    row = conn.execute(
        """SELECT order_id, transaction_id, status, created_at
           FROM orders
           WHERE transaction_id IS NOT NULL
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

if not row:
    raise SystemExit("No LINE Pay transaction found")

order_id, transaction_id, local_status, created_at = row
uri = f"/v4/payments/requests/{transaction_id}/check"
nonce = str(uuid.uuid4())
message = channel_secret + uri + nonce
digest = hmac.new(
    channel_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
).digest()

response = requests.get(
    api_base + uri,
    headers={
        "Content-Type": "application/json",
        "X-LINE-ChannelId": channel_id,
        "X-LINE-Authorization-Nonce": nonce,
        "X-LINE-Authorization": base64.b64encode(digest).decode("ascii"),
    },
    timeout=25,
)
response.raise_for_status()
result = response.json()

print(
    json.dumps(
        {
            "orderId": order_id,
            "transactionId": transaction_id,
            "localStatus": local_status,
            "createdAtEpoch": created_at,
            "linePayCode": result.get("returnCode"),
            "linePayMessage": result.get("returnMessage"),
        },
        ensure_ascii=False,
        indent=2,
    )
)
