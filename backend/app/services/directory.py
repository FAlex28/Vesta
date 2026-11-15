"""Per-device directory: paired phones, emergency contacts, invites and push subscriptions.

Kept in one small JSON file (``VESTA_STATE_DIR/directory.json``) written atomically, so a
Raspberry Pi restart keeps the person's phone pairing and her contacts. Without a state
directory (tests) everything stays in memory. One Vesta process owns the file.

Stored on purpose: contact names and phone numbers chosen by the person, push endpoints,
grant ids. Not stored: grant tokens, codes in clear after use, coordinates, audio.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.errors import AppError, ConflictError, NotFoundError

from .access import generate_code, normalize_code

LOGGER = logging.getLogger("vesta.directory")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().-]{5,24}$")


def _now(value: datetime | None) -> datetime:
    return (value or datetime.now(UTC)).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class InvalidInput(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(code="INVALID_INPUT", message=message, status_code=422)


def normalize_phone(raw: str) -> str:
    text = raw.strip()
    if not _PHONE_RE.match(text):
        raise InvalidInput("Phone number is not valid")
    digits = re.sub(r"[^0-9]", "", text)
    if not 6 <= len(digits) <= 15:
        raise InvalidInput("Phone number is not valid")
    return ("+" if text.startswith("+") else "") + digits


def endpoint_key(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24]


class DeviceDirectory:
    def __init__(self, path: Path | None, *, max_phones: int = 5, max_contacts: int = 5) -> None:
        self._path = path
        self._max_phones = max_phones
        self._max_contacts = max_contacts
        self._state: dict[str, Any] = {
            "version": 1,
            "owner_name": None,
            "device_code": None,
            "vapid": None,
            "phones": {},
            "contacts": {},
            "invites": {},
            "grants": {},
            "pushes": {},
        }
        self._last_saved_touch = datetime.min.replace(tzinfo=UTC)
        if path is not None and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self._state.update({key: loaded[key] for key in self._state if key in loaded})
            except (OSError, ValueError):
                LOGGER.exception("directory file unreadable; starting empty: %s", path)

    # ------------------------------------------------------------------ persistence

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self._path)

    # ------------------------------------------------------------------ device secrets

    def device_code(self, configured: str | None) -> str:
        """The code printed on the device; generated once and kept when not configured."""

        if configured:
            return normalize_code(configured)
        if not self._state["device_code"]:
            self._state["device_code"] = generate_code(10)
            self._save()
        return self._state["device_code"]

    def vapid(self) -> dict[str, str]:
        """VAPID key pair for Web Push, generated once when not configured."""

        if not self._state["vapid"]:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            from .access import _b64

            key = ec.generate_private_key(ec.SECP256R1())
            private_raw = key.private_numbers().private_value.to_bytes(32, "big")
            public_raw = key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            self._state["vapid"] = {"private_key": _b64(private_raw), "public_key": _b64(public_raw)}
            self._save()
        return dict(self._state["vapid"])

    # ------------------------------------------------------------------ owner

    def owner_name(self) -> str | None:
        return self._state["owner_name"]

    def set_owner_name(self, name: str | None) -> None:
        cleaned = (name or "").strip()[:60] or None
        self._state["owner_name"] = cleaned
        self._save()

    # ------------------------------------------------------------------ phones

    def register_phone(self, token_id: str, label: str | None, *, now: datetime | None = None) -> dict:
        active = [phone for phone in self._state["phones"].values() if not phone["revoked_at"]]
        if len(active) >= self._max_phones:
            raise ConflictError(
                "TOO_MANY_PHONES",
                "This device already has the maximum number of paired phones; revoke one first",
            )
        instant = _iso(_now(now))
        phone_id = f"ph-{secrets.token_hex(4)}"
        self._state["phones"][phone_id] = {
            "id": phone_id,
            "label": (label or "Telefon").strip()[:60] or "Telefon",
            "created_at": instant,
            "last_seen_at": instant,
            "revoked_at": None,
        }
        self._state["grants"][token_id] = {"kind": "phone", "owner": phone_id}
        self._save()
        return copy.deepcopy(self._state["phones"][phone_id])

    def phone_for_grant(self, token_id: str, *, now: datetime | None = None) -> dict | None:
        link = self._state["grants"].get(token_id)
        if not link or link["kind"] != "phone":
            return None
        phone = self._state["phones"].get(link["owner"])
        if not phone or phone["revoked_at"]:
            return None
        instant = _now(now)
        phone["last_seen_at"] = _iso(instant)
        if instant - self._last_saved_touch > timedelta(minutes=1):
            self._last_saved_touch = instant
            self._save()
        return copy.deepcopy(phone)

    def phones(self) -> list[dict]:
        return [copy.deepcopy(p) for p in self._state["phones"].values() if not p["revoked_at"]]

    def revoke_phone(self, phone_id: str, *, now: datetime | None = None) -> None:
        phone = self._state["phones"].get(phone_id)
        if not phone or phone["revoked_at"]:
            raise NotFoundError("Phone not found")
        phone["revoked_at"] = _iso(_now(now))
        self._save()

    def revoke_all_phones(self, *, now: datetime | None = None) -> int:
        count = 0
        for phone in self._state["phones"].values():
            if not phone["revoked_at"]:
                phone["revoked_at"] = _iso(_now(now))
                count += 1
        self._save()
        return count

    # ------------------------------------------------------------------ contacts

    def add_contact(self, name: str, phone: str, *, now: datetime | None = None) -> dict:
        cleaned_name = name.strip()[:60]
        if not cleaned_name:
            raise InvalidInput("Contact name is required")
        number = normalize_phone(phone)
        active = [c for c in self._state["contacts"].values() if not c["revoked_at"]]
        if any(c["phone"] == number for c in active):
            raise ConflictError("CONTACT_EXISTS", "This phone number is already an emergency contact")
        if len(active) >= self._max_contacts:
            raise ConflictError("TOO_MANY_CONTACTS", "Maximum number of emergency contacts reached")
        contact_id = f"ct-{secrets.token_hex(4)}"
        self._state["contacts"][contact_id] = {
            "id": contact_id,
            "name": cleaned_name,
            "phone": number,
            "created_at": _iso(_now(now)),
            "accepted_at": None,
            "revoked_at": None,
            "subscriptions": {},
        }
        self._save()
        return self.contact_view(contact_id)

    def contact_view(self, contact_id: str) -> dict:
        contact = self._state["contacts"][contact_id]
        return {
            "id": contact["id"],
            "name": contact["name"],
            "phone": contact["phone"],
            "created_at": contact["created_at"],
            "accepted_at": contact["accepted_at"],
            "push_enabled": bool(contact["subscriptions"]),
            "invite_pending": any(
                invite["contact_id"] == contact_id for invite in self._state["invites"].values()
            ),
        }

    def contacts(self) -> list[dict]:
        return [self.contact_view(cid) for cid, c in self._state["contacts"].items() if not c["revoked_at"]]

    def _active_contact(self, contact_id: str) -> dict:
        contact = self._state["contacts"].get(contact_id)
        if not contact or contact["revoked_at"]:
            raise NotFoundError("Contact not found")
        return contact

    def revoke_contact(self, contact_id: str, *, now: datetime | None = None) -> None:
        contact = self._active_contact(contact_id)
        contact["revoked_at"] = _iso(_now(now))
        contact["subscriptions"] = {}
        for code in [c for c, invite in self._state["invites"].items() if invite["contact_id"] == contact_id]:
            del self._state["invites"][code]
        self._save()

    def create_invite(self, contact_id: str, *, ttl: timedelta, now: datetime | None = None) -> tuple[str, datetime]:
        self._active_contact(contact_id)
        instant = _now(now)
        for code in [c for c, invite in self._state["invites"].items() if invite["contact_id"] == contact_id]:
            del self._state["invites"][code]
        while True:
            code = generate_code(8)
            if code not in self._state["invites"]:
                break
        expires = instant + ttl
        self._state["invites"][code] = {"contact_id": contact_id, "expires_at": _iso(expires)}
        self._save()
        return code, expires

    def claim_invite(self, raw_code: str, token_id: str, *, now: datetime | None = None) -> dict | None:
        """One-time: returns the contact, or None for an unknown/expired code."""

        instant = _now(now)
        expired = [c for c, i in self._state["invites"].items() if _parse(i["expires_at"]) <= instant]
        for code in expired:
            del self._state["invites"][code]
        invite = self._state["invites"].pop(normalize_code(raw_code), None)
        if invite is None:
            if expired:
                self._save()
            return None
        contact = self._state["contacts"].get(invite["contact_id"])
        if not contact or contact["revoked_at"]:
            self._save()
            return None
        contact["accepted_at"] = contact["accepted_at"] or _iso(instant)
        self._state["grants"][token_id] = {"kind": "contact", "owner": contact["id"]}
        self._save()
        return self.contact_view(contact["id"])

    def contact_for_grant(self, token_id: str) -> dict | None:
        link = self._state["grants"].get(token_id)
        if not link or link["kind"] != "contact":
            return None
        contact = self._state["contacts"].get(link["owner"])
        if not contact or contact["revoked_at"]:
            return None
        return self.contact_view(contact["id"])

    # ------------------------------------------------------------------ open access (demo)

    OPEN_PHONE_ID = "ph-open"
    OPEN_CONTACT_ID = "ct-open"

    def register_open_phone(self, token_id: str, *, now: datetime | None = None) -> dict:
        """VESTA_OPEN_ACCESS: the person's phone, without any pairing code."""

        instant = _iso(_now(now))
        phone = self._state["phones"].get(self.OPEN_PHONE_ID)
        if phone is None:
            phone = self._state["phones"][self.OPEN_PHONE_ID] = {
                "id": self.OPEN_PHONE_ID,
                "label": "Telefonul persoanei",
                "created_at": instant,
                "last_seen_at": instant,
                "revoked_at": None,
            }
        phone["revoked_at"] = None
        phone["last_seen_at"] = instant
        self._state["grants"][token_id] = {"kind": "phone", "owner": self.OPEN_PHONE_ID}
        self._save()
        return copy.deepcopy(phone)

    def register_open_contact(self, token_id: str, *, now: datetime | None = None) -> dict:
        """VESTA_OPEN_ACCESS: the trusted contact's page, without any invitation code."""

        instant = _iso(_now(now))
        contact = self._state["contacts"].get(self.OPEN_CONTACT_ID)
        if contact is None:
            contact = self._state["contacts"][self.OPEN_CONTACT_ID] = {
                "id": self.OPEN_CONTACT_ID,
                "name": "Persoana de încredere",
                "phone": "",
                "created_at": instant,
                "accepted_at": instant,
                "revoked_at": None,
                "subscriptions": {},
            }
        contact["revoked_at"] = None
        self._state["grants"][token_id] = {"kind": "contact", "owner": self.OPEN_CONTACT_ID}
        self._save()
        return self.contact_view(self.OPEN_CONTACT_ID)

    # ------------------------------------------------------------------ push

    def add_subscription(self, contact_id: str, subscription: dict, *, now: datetime | None = None) -> None:
        contact = self._active_contact(contact_id)
        endpoint = subscription["endpoint"]
        contact["subscriptions"][endpoint_key(endpoint)] = {
            "endpoint": endpoint,
            "keys": {"p256dh": subscription["keys"]["p256dh"], "auth": subscription["keys"]["auth"]},
            "created_at": _iso(_now(now)),
        }
        self._save()

    def remove_subscription(self, endpoint: str) -> None:
        key = endpoint_key(endpoint)
        changed = False
        for contact in self._state["contacts"].values():
            if contact["subscriptions"].pop(key, None) is not None:
                changed = True
        if changed:
            self._save()

    def push_targets(self) -> list[tuple[str, str, dict]]:
        """(contact_id, endpoint_key, subscription_info) for every active contact."""

        targets = []
        for contact in self._state["contacts"].values():
            if contact["revoked_at"]:
                continue
            for key, sub in contact["subscriptions"].items():
                targets.append((contact["id"], key, {"endpoint": sub["endpoint"], "keys": dict(sub["keys"])}))
        return targets

    def push_record(self, key: str) -> dict | None:
        record = self._state["pushes"].get(key)
        return copy.deepcopy(record) if record else None

    def set_push_record(self, key: str, record: dict) -> None:
        self._state["pushes"][key] = record
        if len(self._state["pushes"]) > 500:
            for old in sorted(self._state["pushes"], key=lambda k: self._state["pushes"][k].get("at", ""))[:100]:
                del self._state["pushes"][old]
        self._save()

    def push_summary(self, alert_id: str) -> dict[str, str]:
        """kind -> best status for one alert ("SENT" beats "FAILED")."""

        summary: dict[str, str] = {}
        for record in self._state["pushes"].values():
            if record.get("alert_id") != alert_id:
                continue
            kind = record["kind"]
            if summary.get(kind) != "SENT":
                summary[kind] = record["status"]
        return summary
