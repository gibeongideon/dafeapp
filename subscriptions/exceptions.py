class SubscriptionError(Exception):
    """Subscription is inactive, expired, suspended, or cancelled."""


class SubscriptionLimitError(SubscriptionError):
    """A plan-specific limit has been reached."""
