# Subscriptions

Handles plan management, subscription lifecycle, and Paystack payment processing for DafeApp organizations.

---

## Models

| Model | Purpose |
|---|---|
| `Plan` | Defines tiers (STARTER / GROWTH / ENTERPRISE) with limits and pricing |
| `Subscription` | One-per-org record tracking status and billing period |
| `PaystackPayment` | Audit trail for every Paystack transaction |
| `UsageRecord` | Metered usage events (backups, staging, upgrades) |

---

## Paystack Integration

### How it works

1. User visits **Billing** (`/subscriptions/billing/`) and clicks **Subscribe** or **Upgrade** on a plan.
2. A `PENDING` `PaystackPayment` record is created and the user is redirected to the Paystack hosted checkout.
3. After payment Paystack redirects back to the **callback URL**, which verifies the transaction and activates the subscription.
4. **Webhooks** keep the subscription status in sync for renewals, cancellations, and payment failures.

```
User clicks Subscribe
        │
        ▼
POST /subscriptions/payment/initiate/<plan_id>/
        │  creates PaystackPayment (PENDING)
        │  calls Paystack /transaction/initialize
        ▼
Paystack hosted checkout
        │
        ▼
GET /subscriptions/payment/callback/?reference=DAFE-XXXX
        │  calls Paystack /transaction/verify/:reference
        │  on success → Subscription.status = ACTIVE
        ▼
/subscriptions/payment/success/

Async: Paystack → POST /subscriptions/webhook/paystack/
        renewals, cancellations, payment failures
```

### Environment variables

Add these to your `.env` file (see `.env.example` for the full list):

```bash
# Get from https://dashboard.paystack.com/#/settings/developer
PAYSTACK_SECRET_KEY=your_paystack_secret_key
PAYSTACK_PUBLIC_KEY=your_paystack_public_key

# Currency subunit used for amounts sent to Paystack
# USD = cents (default), NGN = kobo, GHS = pesewas, ZAR = cents
PAYSTACK_CURRENCY=USD
```

Use Paystack test keys during development. Paystack test mode accepts card number `4084084084084081`.

### Paystack dashboard setup

**1. Register the webhook URL**

Go to [Settings → API Keys & Webhooks](https://dashboard.paystack.com/#/settings/developer) and add:

```
https://yourdomain.com/subscriptions/webhook/paystack/
```

Events to enable: `charge.success`, `subscription.create`, `subscription.disable`, `invoice.payment_failed`.

**2. Create Paystack plans (for recurring billing)**

For each paid `Plan` in Django admin, create a matching plan in the Paystack dashboard:

- Go to **Products → Plans → Create Plan**
- Set interval to **Monthly**, amount in the correct subunit (e.g. 5000 for NGN 50)
- Copy the generated **Plan Code** (e.g. `PLN_xxxxxxxxxxxxxxx`)

Then in Django admin (`/admin/subscriptions/plan/`), paste the Plan Code into the **Paystack plan code** field on the corresponding plan. When this field is set, subscribing automatically creates a recurring Paystack subscription.

### URL reference

| URL | View | Notes |
|---|---|---|
| `POST /subscriptions/payment/initiate/<plan_id>/` | `InitiatePaymentView` | Starts checkout; redirects to Paystack |
| `GET /subscriptions/payment/callback/` | `PaymentCallbackView` | Paystack redirect after payment |
| `GET /subscriptions/payment/success/` | `PaymentSuccessView` | Confirmation page |
| `GET /subscriptions/payment/failed/` | `PaymentFailedView` | Failure page |
| `POST /subscriptions/cancel/` | `CancelSubscriptionView` | Cancels active subscription |
| `POST /subscriptions/webhook/paystack/` | `PaystackWebhookView` | CSRF-exempt; validates HMAC-SHA512 |

### Webhook events handled

| Event | Action |
|---|---|
| `charge.success` | Activates subscription; records payment as SUCCESS |
| `subscription.create` | Stores `paystack_subscription_code` and `paystack_email_token` on the subscription |
| `subscription.disable` | Sets `Subscription.status = CANCELLED` |
| `invoice.payment_failed` | Sets `Subscription.status = PAST_DUE` |

---

## Subscription lifecycle

```
[new org] → TRIAL (14 days, auto-created by signal)
                │
                ▼ first successful payment
             ACTIVE ←──────────────────────────── renewal charge.success
                │
                ├─ invoice.payment_failed ──────► PAST_DUE
                │                                    │
                │                                    ├─ grace period (3 days) → still serviceable
                │                                    └─ charge.success ──────► ACTIVE
                │
                └─ subscription.disable / cancel ──► CANCELLED
```

`Subscription.is_serviceable` is the single property checked by `SubscriptionMiddleware` and `SubscriptionEnforcer.ensure_active()` before any provisioning action.

---

## SubscriptionEnforcer

The enforcer is attached to every request by `SubscriptionMiddleware` as `request.subscription_enforcer`. Use it in views and Celery tasks:

```python
enforcer = request.subscription_enforcer
# or: enforcer = SubscriptionEnforcer(org)

enforcer.ensure_active()           # raises SubscriptionError if not serviceable
enforcer.check_instance_limit()    # raises SubscriptionLimitError if at cap
enforcer.check_backup_limit()
enforcer.check_staging_allowed()
enforcer.check_upgrade_allowed()

enforcer.record_usage(UsageRecord.UsageType.BACKUP)
```

---

## PaystackClient

A thin wrapper around the Paystack REST API (`subscriptions/paystack.py`):

```python
from subscriptions.paystack import PaystackClient, generate_reference

client = PaystackClient()

# Initialize a transaction
data = client.initialize_transaction(
    email="user@example.com",
    amount_kobo=5000,           # NGN 50
    reference=generate_reference(),
    callback_url="https://yourdomain.com/subscriptions/payment/callback/",
    plan_code="PLN_xxxx",       # optional — enables recurring subscription
)
print(data["authorization_url"])

# Verify after redirect
tx = client.verify_transaction("DAFE-XXXX")
assert tx["status"] == "success"

# Create a Paystack plan
plan_data = client.create_plan("Growth Monthly", interval="monthly", amount_kobo=50000)
print(plan_data["plan_code"])  # save this to Plan.paystack_plan_code
```
Then just use your actual domain directly:

Field	Value
Test Callback URL	https://dafeapp.com/subscriptions/payment/callback/
Test Webhook URL	https://dafeapp.com/subscriptions/webhook/paystack/
And your .env on the server:


SITE_URL=https://dafeapp.com
PAYSTACK_SECRET_KEY=your_paystack_test_secret_key
PAYSTACK_PUBLIC_KEY=your_paystack_test_public_key
PAYSTACK_CURRENCY=USD
When you're ready to go live, swap the test keys for your live keys - no code changes needed.
