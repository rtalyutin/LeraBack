"""VK messages.send adapter. Construction and contract tests do not call VK."""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VK_API_URL = "https://api.vk.com/method/messages.send"


class VKSendError(RuntimeError):
    pass


class VKCredentialSendError(VKSendError):
    """The community token, rather than one recipient, needs attention."""
    pass


def vk_keyboard(buttons):
    if not buttons:
        return ""
    rows = []
    for item in buttons:
        action = {
            "type": "text",
            "label": str(item["label"])[:40],
            "payload": json.dumps(item.get("payload", {}), ensure_ascii=False, separators=(",", ":")),
        }
        rows.append([{"action": action, "color": "primary"}])
    return json.dumps({"inline": True, "buttons": rows}, ensure_ascii=False, separators=(",", ":"))


def vk_master_carousel(cards, photo_ids):
    """Use uploaded community photo IDs; no fake VK IDs are ever sent."""
    if not cards or any(str(card["master_id"]) not in photo_ids for card in cards):
        return ""
    elements = []
    for card in cards:
        photo_id = photo_ids[str(card["master_id"])]
        if not re.fullmatch(r"-?\d+_\d+", photo_id):
            raise ValueError("Invalid VK photo_id")
        item = card["button"]
        elements.append({
            "title": str(card["title"])[:80], "photo_id": photo_id,
            "action": {"type": "open_photo"},
            "buttons": [{"action": {"type": "text", "label": "Выбрать",
                                      "payload": json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":"))}}],
        })
    return json.dumps({"type": "carousel", "elements": elements}, ensure_ascii=False, separators=(",", ":"))


class VKSender:
    def __init__(self, access_token, api_version="5.199", timeout=10, opener=urlopen, master_photo_ids=None):
        if not access_token:
            raise ValueError("VK community access token required")
        self.access_token = access_token
        self.api_version = api_version
        self.timeout = timeout
        self.opener = opener
        self.master_photo_ids = master_photo_ids or {}

    def __call__(self, vk_id, text, keyboard, random_id):
        fields = {
            "access_token": self.access_token,
            "v": self.api_version,
            "peer_id": int(vk_id),
            "random_id": int(random_id),
            "message": text,
        }
        if keyboard and all("avatar_master_id" in item for item in keyboard):
            cards = [{"master_id": item["avatar_master_id"], "title": item["label"], "button": item}
                     for item in keyboard]
            carousel = vk_master_carousel(cards, self.master_photo_ids)
            if carousel:
                fields["template"] = carousel
            keyboard = [] if carousel else keyboard
        encoded_keyboard = vk_keyboard(keyboard)
        if encoded_keyboard:
            fields["keyboard"] = encoded_keyboard
        request = Request(
            VK_API_URL,
            data=urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "vk-salon-booking/0.3"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:
            raise VKSendError(f"VK transport failed: {type(exc).__name__}") from exc
        if "error" in payload:
            error = payload.get("error") or {}
            if error.get("error_code") in (5, 7, 27, 28):
                raise VKCredentialSendError("VK community credentials rejected")
            raise VKSendError(f"VK API error {error.get('error_code', 'unknown')}")
        if "response" not in payload:
            raise VKSendError("VK response has no result")
        return payload["response"]
