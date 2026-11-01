from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, NamedTuple

from curl_cffi import requests as _cffi_requests


BASE = "https://www.paypay.ne.jp"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


class PayPayError(Exception):
    pass


class PayPayLoginError(PayPayError):
    pass


class PayPayNetworkError(PayPayError):
    pass


class PayPayAcceptError(PayPayError):
    pass


class PayPayAcceptUnknownError(PayPayNetworkError):
    pass


class PayPayLinkGoneError(PayPayError):
    pass


def _dict_at(data: Any, key: str) -> dict:
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == int(value):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


LINK_DEAD_STATES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "DECLINED", "FAILED"})

LINK_RECEIVED_STATES = frozenset({"COMPLETED", "SUCCESS", "SUCCEEDED", "DONE", "RECEIVED", "ACCEPTED"})

LINK_DONE_STATES = LINK_RECEIVED_STATES | LINK_DEAD_STATES


def _link_states(raw: Any) -> set[str]:
    payload = _dict_at(raw, "payload")
    data = _dict_at(_dict_at(payload, "message"), "data")
    return {str(payload.get("orderStatus") or "").upper(), str(data.get("status") or "").upper()} - {""}


def _is_pending_link(raw: Any) -> bool:
    payload = _dict_at(raw, "payload")
    data = _dict_at(_dict_at(payload, "message"), "data")
    return (
        str(payload.get("orderStatus") or "").upper() == "PENDING"
        or str(data.get("status") or "").upper() == "PENDING"
    )


def is_link_dead(raw: Any) -> bool:
    """リンクが終了状態（キャンセル・期限切れ・辞退など）か？保留ループを止めるのに使う。"""
    return bool(_link_states(raw) & LINK_DEAD_STATES)


def is_link_received_by_me(raw: Any) -> bool:
    """このアカウントがもう受け取ったか？link_check の応答に 2026-10 から入った
    `receiver.isReceiver` を使う。履歴検索より速くて確実。保留の確認はまずこれで。"""
    payload = _dict_at(raw, "payload")
    if _dict_at(payload, "receiver").get("isReceiver") is not True:
        return False
    states = _link_states(raw)
    return bool(states & LINK_RECEIVED_STATES) and not (states & LINK_DEAD_STATES)


def is_link_pending(raw: Any) -> bool:
    """リンクがまだ受け取り待ち（PENDING）か？保留中に受け取りをやり直すか決めるのに使う。"""
    return _is_pending_link(raw)


TEMP_HOLD_CODE = "42007013"


def is_temp_hold(exc: BaseException) -> bool:
    """PayPay側の「一時保留」（42007013）で断られたか？
    これは失敗ではなく保留。少し待つと受け取れるようになるので、受け取りをやり直す。"""
    text = str(exc).lower()
    return TEMP_HOLD_CODE in text or "half sheet" in text


def _flat(entry: Any, paths: list[str]) -> list:
    out = []
    for path in paths:
        v: Any = entry
        for k in path.split("."):
            v = v.get(k) if isinstance(v, dict) else None
        if v is not None and v != "" and not isinstance(v, (dict, list)):
            out.append(v)
    return out


def history_entry_matches_receive(entry: dict, order_id: str, amount: int) -> bool:
    """履歴1件が「注文 order_id、金額 amount の P2P受け取り完了」か？"""
    ids = {str(v) for v in _flat(entry, ["orderId", "orderInfo.orderId"])}
    if str(order_id) not in ids:
        return False
    types = {str(v).upper() for v in _flat(entry, ["orderType", "orderInfo.orderType"])}
    if types != {"P2PRECEIVE"}:
        return False
    states = {
        str(v).upper()
        for v in _flat(
            entry,
            ["orderStatus", "statusInfo.orderStatus", "orderInfo.orderStatus", "orderInfo.statusInfo.orderStatus", "status"],
        )
    }
    if not (states & LINK_RECEIVED_STATES):
        return False
    amounts = {
        _as_int(v)
        for v in _flat(
            entry,
            [
                "totalAmount",
                "amount",
                "amount.value",
                "amountDetail.amount.value",
                "participantDetails.amountDetail.amount.value",
            ],
        )
    }
    return amount in amounts


def find_receive_in_history(history: dict, order_id: str, amount: int) -> bool:
    """取引履歴の応答から、注文 order_id・金額 amount の受け取り完了を探す。
    `isReceiver` が無い古い応答や、保留が長引いたときの最終確認に使う。"""
    payload = _dict_at(history, "payload")
    entries = payload.get("paymentInfoList")
    if not isinstance(entries, list):
        return False
    return any(isinstance(e, dict) and history_entry_matches_receive(e, order_id, amount) for e in entries)


class LinkInfo(NamedTuple):
    sender_name: str
    sender_external_id: str
    order_id: str
    chat_room_id: str
    amount: int
    status: str
    has_password: bool
    raw: dict


class BalanceInfo(NamedTuple):
    all_balance: int
    useable_balance: int
    money: int
    money_light: int
    points: int
    raw: dict


class PayPay:
    """Web版PayPayのクライアント。

    最初はSMS認証で入る必要があるが、同じclient_uuidを保存して使い回せば、
    次からはパスワードだけで入れる（PayPayが「登録済み端末」として扱う）。
    鍵は1日ほどで切れるが、expires_in の時刻を見て、切れたら自動で入り直せる。
    """

    def __init__(
        self,
        client_uuid: str | None = None,
        cookies: list[dict] | None = None,
        access_token: str | None = None,
        proxy: str | dict | None = None,
    ):
        self.session = _cffi_requests.Session()
        self.client_uuid = client_uuid or str(uuid.uuid4())
        self.access_token = access_token
        self.token_response: dict = {}
        self.otp_prefix: str | None = None
        self.otp_reference_id: str | None = None
        self.phone: str | None = None
        self.password: str | None = None
        self.saved_at: float = 0.0
        self.expires_in: float = 0.0

        if isinstance(proxy, str):
            if "://" not in proxy:
                proxy = "http://" + proxy
            self.proxy: dict | None = {"http": proxy, "https": proxy}
        else:
            self.proxy = proxy

        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": f"{BASE}/app/",
            "User-Agent": USER_AGENT,
        }
        if cookies:
            self.import_cookies(cookies)

    def import_cookies(self, cookies: list[dict]) -> None:
        for c in cookies:
            if isinstance(c, dict) and c.get("name"):
                self.session.cookies.set(
                    c["name"], c.get("value") or "", domain=c.get("domain") or "", path=c.get("path") or "/"
                )

    def export_cookies(self) -> list[dict]:
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self.session.cookies.jar
        ]

    def _send(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        try:
            response = self.session.request(
                method,
                BASE + path,
                headers=self.headers,
                params=params,
                json=body,
                proxies=self.proxy,
                timeout=30,
            )
        except Exception as e:
            raise PayPayNetworkError(f"PayPayに接続できませんでした: {e!r}") from e
        if response.status_code == 202 and "x-amzn-waf-action" in response.headers:
            raise PayPayNetworkError("PayPay側のボット確認（AWS WAF）が出たため接続できませんでした")
        try:
            data = response.json()
        except Exception as e:
            raise PayPayNetworkError(f"PayPayの応答を読めませんでした（HTTP {response.status_code}）") from e
        if not isinstance(data, dict):
            raise PayPayNetworkError(f"PayPayの応答が想定外です: {str(data)[:200]}")
        return response.status_code, data

    def _api(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        status, data = self._send(method, path, params=params, body=body)
        code = _dict_at(data, "header").get("resultCode")
        if status == 401 or code == "S0001":
            raise PayPayLoginError(data)
        if code != "S0000":
            raise PayPayError(data)
        return data

    def _token(self, body: dict) -> dict:
        status, data = self._send("POST", "/app/v1/oauth/token", body=body)
        if status >= 400 or _dict_at(data, "result_info").get("result_status") == "F":
            raise PayPayLoginError(data)
        return data

    def _take_token(self, data: dict) -> bool:
        token = data.get("access_token")
        if not token:
            return False
        self.access_token = token
        self.token_response = data
        self.saved_at = time.time()
        try:
            self.expires_in = float(data.get("expires_in") or 0.0)
        except (TypeError, ValueError):
            self.expires_in = 0.0
        return True

    def start_login(self, phone: str, password: str) -> str | None:
        """パスワードを送る。戻り値：
        - None なら、すでに入れた（SMSなし）。verify_otp は不要
        - 文字列（"ZE" など）なら、SMSが届くのでその4桁+この文字列を verify_otp に渡す
        """
        phone = "".join(ch for ch in str(phone) if ch.isdigit())
        data = self._token(
            {
                "scope": "SIGN_IN",
                "client_uuid": self.client_uuid,
                "grant_type": "password",
                "username": phone,
                "password": password,
                "add_otp_prefix": True,
                "language": "ja",
            }
        )
        self.phone = phone
        self.password = password
        if self._take_token(data):
            return None
        if not data.get("otp_reference_id"):
            raise PayPayLoginError(data)
        self.otp_reference_id = data["otp_reference_id"]
        self.otp_prefix = data.get("otp_prefix") or ""
        return self.otp_prefix

    def resend_otp(self) -> None:
        if not self.otp_reference_id:
            raise PayPayLoginError("先に start_login を呼んでください")
        self._send("POST", f"/app/v1/otp/mobile/resend/{self.otp_reference_id}")

    def verify_otp(self, otp: str) -> dict:
        if not self.otp_reference_id:
            raise PayPayLoginError("先に start_login を呼んでください")
        digits = "".join(ch for ch in str(otp) if ch.isdigit())
        if len(digits) != 4:
            raise PayPayLoginError("SMSに書かれた4桁の数字を入れてください")
        data = self._token(
            {
                "scope": "SIGN_IN",
                "grant_type": "otp",
                "client_uuid": self.client_uuid,
                "otp": digits,
                "otp_prefix": self.otp_prefix,
                "otp_reference_id": self.otp_reference_id,
                "username_type": "MOBILE",
                "language": "ja",
            }
        )
        if not self._take_token(data):
            raise PayPayLoginError(data)
        self.otp_reference_id = None
        self.otp_prefix = None
        return data

    def expires_at(self) -> float:
        return self.saved_at + self.expires_in if self.saved_at and self.expires_in else 0.0

    def is_alive(self) -> bool:
        return bool(self.access_token and (self.expires_at() == 0.0 or time.time() < self.expires_at()))

    def get_balance(self) -> BalanceInfo:
        data = self._api(
            "GET",
            "/app/v1/bff/getBalanceInfo",
            params={"includePending": "true", "noCache": "true"},
        )
        p = _dict_at(data, "payload")
        s = _dict_at(p, "walletSummary")
        d = _dict_at(p, "walletDetail")
        return BalanceInfo(
            _dict_at(s, "allTotalBalanceInfo").get("balance") or 0,
            _dict_at(s, "usableBalanceInfoWithoutCashback").get("balance") or 0,
            _dict_at(d, "emoneyBalanceInfo").get("balance") or 0,
            _dict_at(d, "prepaidBalanceInfo").get("balance") or 0,
            _dict_at(d, "cashBackBalanceInfo").get("balance") or 0,
            data,
        )

    def get_history(self, size: int = 20) -> dict:
        return self._api(
            "GET",
            "/app/v3/bff/getPaymentHistory",
            params={
                "pageSize": str(size),
                "orderTypes": "",
                "paymentMethodTypes": "",
                "isOverdraftOnly": "false",
            },
        )

    def get_profile(self) -> dict:
        return self._api("GET", "/app/v1/getUserProfile")

    def _code(self, url_or_code: str) -> str:
        return str(url_or_code).replace("https://pay.paypay.ne.jp/", "").strip("/")

    def link_check(self, url_or_code: str) -> LinkInfo:
        info = self._api(
            "GET",
            "/app/v2/p2p-api/getP2PLinkInfo",
            params={"verificationCode": self._code(url_or_code), "client_uuid": self.client_uuid},
        )
        p = _dict_at(info, "payload")
        sender = _dict_at(p, "sender")
        pending = _dict_at(p, "pendingP2PInfo")
        message = _dict_at(p, "message")
        return LinkInfo(
            sender.get("displayName") or "",
            sender.get("externalId") or "",
            pending.get("orderId") or "",
            message.get("chatRoomId") or "",
            _as_int(pending.get("amount")) or 0,
            str(_dict_at(message, "data").get("status") or p.get("orderStatus") or ""),
            bool(pending.get("isSetPasscode")),
            info,
        )

    def link_receive(
        self,
        url_or_code: str,
        passcode: str | None = None,
        info: LinkInfo | dict | None = None,
    ) -> dict:
        code = self._code(url_or_code)
        if isinstance(info, LinkInfo):
            raw = info.raw
        elif isinstance(info, dict):
            raw = info
        else:
            raw = self._api(
                "GET",
                "/app/v2/p2p-api/getP2PLinkInfo",
                params={"verificationCode": code, "client_uuid": self.client_uuid},
            )
        if not _is_pending_link(raw):
            raise PayPayLinkGoneError(raw)
        p = _dict_at(raw, "payload")
        pending = _dict_at(p, "pendingP2PInfo")
        message = _dict_at(p, "message")
        if pending.get("isSetPasscode") and passcode is None:
            raise PayPayError("このリンクにはパスコードが設定されています")
        body = {
            "requestAt": pending.get("createdAt"),
            "orderId": pending.get("orderId"),
            "verificationCode": code,
            "requestId": str(uuid.uuid4()),
            "senderMessageId": message.get("messageId"),
            "senderChannelUrl": message.get("chatRoomId"),
            "iosMinimumVersion": message.get("iosMinimumVersion") or "3.45.0",
            "androidMinimumVersion": message.get("androidMinimumVersion") or "3.45.0",
            "client_uuid": self.client_uuid,
        }
        if pending.get("isSetPasscode"):
            body["passcode"] = str(passcode)
        try:
            status, data = self._send("POST", "/app/v2/p2p-api/acceptP2PSendMoneyLink", body=body)
        except PayPayNetworkError as e:
            raise PayPayAcceptUnknownError(f"受け取りの応答を確認できませんでした: {e}") from e
        code_r = _dict_at(data, "header").get("resultCode")
        if status == 401 or code_r == "S0001":
            raise PayPayLoginError(data)
        if code_r != "S0000":
            raise PayPayAcceptError(data)
        return data

    def to_dict(self) -> dict:
        return {
            "phone": self.phone,
            "password": self.password,
            "client_uuid": self.client_uuid,
            "access_token": self.access_token,
            "cookies": self.export_cookies(),
            "saved_at": self.saved_at,
            "expires_in": self.expires_in,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PayPay":
        c = cls(
            client_uuid=data.get("client_uuid"),
            cookies=data.get("cookies") or None,
            access_token=data.get("access_token"),
        )
        c.phone = data.get("phone")
        c.password = data.get("password")
        try:
            c.saved_at = float(data.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            c.saved_at = 0.0
        try:
            c.expires_in = float(data.get("expires_in") or 0.0)
        except (TypeError, ValueError):
            c.expires_in = 0.0
        return c

    def save(self, path: str | os.PathLike) -> None:
        """JSONファイルに今の状態（電話番号・パスワード・uuid・クッキー）を書き出す。
        次回は PayPay.load(path) で、SMSを飛ばして入り直せる。
        """
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "PayPay | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return cls.from_dict(data)

    def relogin(self, password: str | None = None) -> str | None:
        """同じ client_uuid を使い、保存されたパスワード（または渡したパスワード）で入り直す。
        PayPayが「登録済み端末」と判断すればSMSなしで入れる（戻り値 None）。
        SMSを求められたら、戻り値は otp_prefix。verify_otp(数字) を呼ぶ。
        """
        if not self.phone:
            raise PayPayLoginError("電話番号を保存していません。start_login で入ってください")
        pw = password or self.password
        if not pw:
            raise PayPayLoginError("パスワードを保存していません。start_login で入ってください")
        return self.start_login(self.phone, pw)

    def ensure_alive(
        self,
        password: str | None = None,
        otp_input: Callable[[str], str] | None = None,
    ) -> None:
        """期限が切れていたら入り直す。SMSが要るときは otp_input（例：input）が呼ばれる。"""
        if self.is_alive():
            return
        prefix = self.relogin(password)
        if prefix is None:
            return
        if otp_input is None:
            raise PayPayLoginError("SMS認証が必要です（otp_input を渡してください）")
        self.verify_otp(otp_input(prefix))
