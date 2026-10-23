"""LINE Messaging API 用到的那幾支：驗簽、回覆、查名字。

刻意不裝 line-bot-sdk：這裡只需要三支 HTTP 呼叫跟一個 HMAC，SDK 的體積與它
跟著 API 版本換來換去的介面都不值得。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"
TIMEOUT_SECONDS = 5
# LINE 一次回覆的文字上限是 5000 字。
TEXT_LIMIT = 5000


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """X-Line-Signature = base64(HMAC-SHA256(channel secret, 原始 request body))。"""
    if not channel_secret or not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), signature)


class LineClient:
    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def reply_text(self, reply_token: str, text: str) -> None:
        if not self.access_token:
            logger.warning("沒有設定 LINE_CHANNEL_ACCESS_TOKEN，略過回覆：%s", text)
            return
        response = requests.post(
            f"{API_BASE}/message/reply",
            headers=self._headers(),
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:TEXT_LIMIT]}]},
            timeout=TIMEOUT_SECONDS,
        )
        if not response.ok:
            logger.error("LINE 回覆失敗 %s：%s", response.status_code, response.text)

    def display_name(self, source: dict) -> str | None:
        """發話者在這個聊天室裡的名字。查不到（token 沒設、對方擋了、API 掛了）
        回 None，由呼叫端決定用什麼代替。"""
        user_id = source.get("userId")
        if not user_id:
            return None
        if source.get("type") == "group":
            path = f"/group/{source['groupId']}/member/{user_id}"
        elif source.get("type") == "room":
            path = f"/room/{source['roomId']}/member/{user_id}"
        else:
            path = f"/profile/{user_id}"
        return self._get(path, "displayName")

    def group_name(self, group_id: str) -> str | None:
        return self._get(f"/group/{group_id}/summary", "groupName")

    def _get(self, path: str, field: str) -> str | None:
        if not self.access_token:
            return None
        try:
            response = requests.get(f"{API_BASE}{path}", headers=self._headers(), timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.error("LINE API %s 連線失敗：%s", path, exc)
            return None
        if not response.ok:
            logger.warning("LINE API %s 回 %s：%s", path, response.status_code, response.text)
            return None
        return response.json().get(field)
