"""Praguri configurabile prin variabile de mediu. Valorile implicite sunt pentru demo,
nu standarde validate de siguranță."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class DbSettings:
    cancel_window_s: float = 5.0            # VESTA_CANCEL_WINDOW_SECONDS
    checkin_timeout_s: float = 15.0         # VESTA_CHECKIN_TIMEOUT_SECONDS
    heartbeat_interval_s: float = 15.0      # VESTA_HEARTBEAT_INTERVAL_SECONDS
    heartbeat_disconnect_s: float = 45.0    # VESTA_HEARTBEAT_DISCONNECT_SECONDS
    gps_stale_s: float = 60.0               # VESTA_GPS_STALE_SECONDS
    gps_low_accuracy_m: float = 100.0       # VESTA_GPS_LOW_ACCURACY_M
    location_retention_days: float = 7.0    # VESTA_LOCATION_RETENTION_DAYS
    notify_max_attempts: int = 5            # VESTA_NOTIFY_MAX_ATTEMPTS
    notify_backoff_s: float = 2.0           # VESTA_NOTIFY_BACKOFF_SECONDS (se dublează, max 5 min)
    token_ttl_minutes: float = 720.0        # VESTA_TOKEN_TTL_MINUTES

    @property
    def heartbeat_reconnecting_s(self) -> float:
        """După două heartbeaturi ratate telefonul trece în RECONNECTING."""
        return min(self.heartbeat_interval_s * 2, self.heartbeat_disconnect_s)

    @classmethod
    def from_env(cls) -> "DbSettings":
        return cls(
            cancel_window_s=_f("VESTA_CANCEL_WINDOW_SECONDS", 5.0),
            checkin_timeout_s=_f("VESTA_CHECKIN_TIMEOUT_SECONDS", 15.0),
            heartbeat_interval_s=_f("VESTA_HEARTBEAT_INTERVAL_SECONDS", 15.0),
            heartbeat_disconnect_s=_f("VESTA_HEARTBEAT_DISCONNECT_SECONDS", 45.0),
            gps_stale_s=_f("VESTA_GPS_STALE_SECONDS", 60.0),
            gps_low_accuracy_m=_f("VESTA_GPS_LOW_ACCURACY_M", 100.0),
            location_retention_days=_f("VESTA_LOCATION_RETENTION_DAYS", 7.0),
            notify_max_attempts=int(_f("VESTA_NOTIFY_MAX_ATTEMPTS", 5)),
            notify_backoff_s=_f("VESTA_NOTIFY_BACKOFF_SECONDS", 2.0),
            token_ttl_minutes=_f("VESTA_TOKEN_TTL_MINUTES", 720.0),
        )
