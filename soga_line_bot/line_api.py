"""LINE Messaging API 用到的那幾支：驗簽、回覆、查名字。

刻意不裝 line-bot-sdk：這裡只需要三支 HTTP 呼叫跟一個 HMAC，SDK 的體積與它
跟著 API 版本換來換去的介面都不值得。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"
TIMEOUT_SECONDS = 5
# LINE 一次回覆的文字上限是 5000 字。
TEXT_LIMIT = 5000

# 回覆文字裡要 @ 某人的地方，用 mention(user_id) 產生的記號佔位；reply_text 送出時
# 換成 LINE 的 textV2 mention。用控制字元當記號，名字或備註裡不可能出現。
_MENTION_MARK = "\x00"
_MENTION = re.compile(_MENTION_MARK + r"(U\w+)" + _MENTION_MARK)


def mention(user_id: str) -> str:
    return f"{_MENTION_MARK}{user_id}{_MENTION_MARK}"


def strip_mentions(text: str) -> str:
    """拿掉 @ 記號（連同前面的空格），變回一般文字。"""
    return re.sub(r" ?" + _MENTION.pattern, "", text)


def _text_v2(text: str) -> dict:
    """把帶 @ 記號的文字轉成 textV2 訊息：記號換成 {m0}、{m1}…，其餘的大括號
    要寫成兩個才不會被當成佔位。"""
    substitution = {}

    def placeholder(match: re.Match) -> str:
        key = f"m{len(substitution)}"
        substitution[key] = {"type": "mention", "mentionee": {"type": "user", "userId": match.group(1)}}
        return "\x01" + key + "\x02"

    marked = _MENTION.sub(placeholder, text)
    escaped = marked.replace("{", "{{").replace("}", "}}").replace("\x01", "{").replace("\x02", "}")
    return {"type": "textV2", "text": escaped[:TEXT_LIMIT], "substitution": substitution}


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
        """回覆一則文字。文字裡有 mention() 記號的話用 textV2 真的 @ 那個人（對方
        會收到通知）；LINE 不收（例如被 @ 的人不在這個群組）就退回一般文字再送一次——
        失敗的請求不會用掉 reply token。"""
        if not self.access_token:
            logger.warning("沒有設定 LINE_CHANNEL_ACCESS_TOKEN，略過回覆：%s", strip_mentions(text))
            return
        if _MENTION.search(text):
            if self._reply(reply_token, _text_v2(text)):
                return
            logger.warning("帶 @ 的回覆失敗，改送一般文字")
        self._reply(reply_token, {"type": "text", "text": strip_mentions(text)[:TEXT_LIMIT]})

    def _reply(self, reply_token: str, message: dict) -> bool:
        response = requests.post(
            f"{API_BASE}/message/reply",
            headers=self._headers(),
            json={"replyToken": reply_token, "messages": [message]},
            timeout=TIMEOUT_SECONDS,
        )
        if not response.ok:
            logger.error("LINE 回覆失敗 %s：%s", response.status_code, response.text)
        return response.ok

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

    def is_member(self, chat_type: str, chat_id: str, user_id: str) -> bool:
        """這個人在不在這個群組（room）裡。查不到一律當作不在。"""
        if not self.access_token or chat_type not in ("group", "room"):
            return False
        try:
            response = requests.get(
                f"{API_BASE}/{chat_type}/{chat_id}/member/{user_id}", headers=self._headers(), timeout=TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            logger.error("查群組成員失敗：%s", exc)
            return False
        return response.ok

    def member_count(self, chat_type: str, chat_id: str) -> int | None:
        value = self._get(f"/{chat_type}/{chat_id}/members/count", "count")
        return int(value) if value is not None else None

    def quota_left(self) -> int | None:
        """這個月還剩幾則推播額度；不限量（付費方案）或查不到回 None。"""
        quota_type = self._get("/message/quota", "type")
        limit = self._get("/message/quota", "value")
        used = self._get("/message/quota/consumption", "totalUsage")
        if quota_type != "limited" or limit is None or used is None:
            return None
        return int(limit) - int(used)

    def push_text(self, to: str, text: str) -> bool:
        """主動發一則到群組（要扣推播額度，按群組人數算）。帶 @ 記號的話用 textV2，
        不收就退回一般文字。"""
        if not self.access_token:
            logger.warning("沒有設定 LINE_CHANNEL_ACCESS_TOKEN，略過推播：%s", strip_mentions(text))
            return False
        messages = [_text_v2(text)] if _MENTION.search(text) else []
        messages.append({"type": "text", "text": strip_mentions(text)[:TEXT_LIMIT]})
        for message in messages:
            response = requests.post(
                f"{API_BASE}/message/push",
                headers=self._headers(),
                json={"to": to, "messages": [message]},
                timeout=TIMEOUT_SECONDS,
            )
            if response.ok:
                return True
            logger.error("LINE 推播失敗 %s：%s", response.status_code, response.text)
        return False

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
