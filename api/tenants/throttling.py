"""
Custom throttling classes for FSec.

Provides stricter rate limiting for authentication endpoints
to mitigate brute-force and credential-stuffing attacks.
"""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class AuthRateThrottle(AnonRateThrottle):
    """
    Stricter throttle for authentication endpoints (login, token obtain).
    Uses the 'auth' rate from DRF settings.
    """
    scope = "auth"


class BurstRateThrottle(UserRateThrottle):
    """
    Burst rate throttle for API endpoints that should be
    protected against rapid-fire requests (e.g., sync trigger).
    """
    scope = "burst"
    THROTTLE_RATES = {"burst": "60/minute"}
