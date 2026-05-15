"""
Thin wrapper around the Paystack REST API.

Usage:
    from subscriptions.paystack import PaystackClient, verify_webhook_signature
    client = PaystackClient()
    result = client.initialize_transaction(email, amount_kobo, reference, callback_url)
"""

import hashlib
import hmac
import uuid

import requests
from django.conf import settings


PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    """Raised when Paystack returns a non-success response."""
    pass


class PaystackClient:
    def __init__(self):
        self.secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        })

    def _post(self, path, payload):
        resp = self.session.post(f"{PAYSTACK_BASE_URL}{path}", json=payload)
        data = resp.json()
        if not data.get("status"):
            raise PaystackError(data.get("message", "Paystack request failed"))
        return data["data"]

    def _get(self, path):
        resp = self.session.get(f"{PAYSTACK_BASE_URL}{path}")
        data = resp.json()
        if not data.get("status"):
            raise PaystackError(data.get("message", "Paystack request failed"))
        return data["data"]

    # ── Transactions ──────────────────────────────────────────────────────────

    def initialize_transaction(self, email, amount_kobo, reference, callback_url,
                                plan_code=None, metadata=None):
        """
        Initialize a Paystack transaction. Returns the Paystack data dict
        with keys: authorization_url, access_code, reference.

        Pass plan_code to create a recurring subscription automatically.
        """
        payload = {
            "email": email,
            "amount": int(amount_kobo),
            "reference": reference,
            "callback_url": callback_url,
        }
        if plan_code:
            payload["plan"] = plan_code
        if metadata:
            payload["metadata"] = metadata
        return self._post("/transaction/initialize", payload)

    def verify_transaction(self, reference):
        """
        Verify a transaction by reference. Returns the full transaction data dict.
        Check data["status"] == "success" for a successful payment.
        """
        return self._get(f"/transaction/verify/{reference}")

    def charge_authorization(self, authorization_code, email, amount_kobo, reference=None):
        """Charge a stored authorization code (recurring debit)."""
        payload = {
            "authorization_code": authorization_code,
            "email": email,
            "amount": int(amount_kobo),
            "reference": reference or generate_reference(),
        }
        return self._post("/transaction/charge_authorization", payload)

    # ── Plans ─────────────────────────────────────────────────────────────────

    def create_plan(self, name, interval, amount_kobo, currency=None):
        """
        Create a Paystack plan. interval: monthly|annually|weekly|daily|quarterly|biannually
        Returns plan data including plan_code.
        """
        payload = {
            "name": name,
            "interval": interval,
            "amount": int(amount_kobo),
            "currency": currency or getattr(settings, "PAYSTACK_CURRENCY", "NGN"),
        }
        return self._post("/plan", payload)

    def fetch_plan(self, plan_code):
        return self._get(f"/plan/{plan_code}")

    # ── Subscriptions ─────────────────────────────────────────────────────────

    def fetch_subscription(self, code):
        return self._get(f"/subscription/{code}")

    def disable_subscription(self, code, token):
        """Disable (cancel) a subscription. token is the email_token from subscription data."""
        return self._post("/subscription/disable", {"code": code, "token": token})

    def enable_subscription(self, code, token):
        return self._post("/subscription/enable", {"code": code, "token": token})

    def get_subscription_manage_link(self, code):
        return self._get(f"/subscription/{code}/manage/link/")

    # ── Customers ─────────────────────────────────────────────────────────────

    def fetch_customer(self, email_or_code):
        return self._get(f"/customer/{email_or_code}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_reference():
    """Generate a unique transaction reference."""
    return f"DAFE-{uuid.uuid4().hex[:16].upper()}"


def verify_webhook_signature(payload_bytes: bytes, signature: str) -> bool:
    """
    Verify Paystack webhook signature.
    Paystack sends HMAC-SHA512 of the raw request body using the secret key.
    """
    secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    computed = hmac.new(
        secret_key.encode("utf-8"),
        payload_bytes,
        digestmod=hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
