"""保留対処つきの受け取りフロー（非同期版）。discord.py など asyncio ベースのBot向け。

同期版 receive_with_hold.py と中身は同じ。sleep と、PayPay への通信を asyncio.to_thread で
別スレッドに逃がしているだけ。Botのイベントループを止めない。
"""

import asyncio
import inspect
import logging

from paypay_web import (
    LINK_RECEIVED_STATES,
    PayPay,
    PayPayAcceptError,
    PayPayAcceptUnknownError,
    PayPayError,
    PayPayLinkGoneError,
    PayPayLoginError,
    find_receive_in_history,
    is_link_dead,
    is_link_pending,
    is_link_received_by_me,
    is_temp_hold,
)


HOLD_RETRY_SECONDS = 15

HOLD_MAX_RETRIES = 160

ACCEPT_RETRY_EVERY = 4

UNCONFIRMED_LIMIT = 4

log = logging.getLogger("paypay_web")


def _try_receive(client: PayPay, url: str, passcode: str | None, info) -> str:
    try:
        client.link_receive(url, passcode, info)
        return "RECEIVED"
    except PayPayLinkGoneError:
        return "GONE"
    except PayPayLoginError:
        return "LOGIN"
    except PayPayAcceptUnknownError:
        return "HOLD"
    except PayPayAcceptError as e:
        return "HOLD" if is_temp_hold(e) else "FAILED"
    except PayPayError:
        return "FAILED"


async def _call_on_hold(on_hold, info) -> None:
    if on_hold is None:
        return
    try:
        result = on_hold(info)
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        log.warning(f"on_hold でエラーが出ました（受け取りの確認は続けます）: {e!r}")


async def receive_with_hold(client: PayPay, url: str, passcode: str | None = None, on_hold=None) -> str:
    """送金リンクを非同期で受け取る。保留になったら、確実な判定が出るまで最大40分繰り返す。

    保留になるのは2つの場合：
      - 受け取りの応答が消えた（受け取れたか分からない）
      - PayPay側の「一時保留」（42007013）で断られた。リンクが受け取り待ちのままなら、1分ごとにやり直す

    on_hold：保留に入ったときに1回だけ呼ばれる。引数は LinkInfo（リンクを読めなかったときは None）。
      async 関数でも普通の関数でもいい。お客さんに「決済が保留中です」と出すのに使う。
      ここでエラーが出ても、受け取りの確認は続ける。

    戻り値：
      "RECEIVED"  成功（受け取り確認済み）
      "GONE"      リンクが別のアカウントに受け取られた・期限切れ・キャンセル（受け取っていない）
      "FAILED"    確認できなかった（人が確認）
      "LOGIN"     ログインが切れている（呼び出し側で入り直してからもう一度）
    """
    accept_sent = False
    order_id, amount = "", 0

    try:
        info = await asyncio.to_thread(client.link_check, url)
    except PayPayLoginError:
        return "LOGIN"
    except PayPayError as e:
        if not is_temp_hold(e):
            return "FAILED"
        info = None

    if info is not None:
        if is_link_received_by_me(info.raw):
            return "RECEIVED"
        if is_link_dead(info.raw):
            return "GONE"
        order_id, amount = info.order_id, info.amount
        result = await asyncio.to_thread(_try_receive, client, url, passcode, info)
        if result != "HOLD":
            return result
        accept_sent = True

    await _call_on_hold(on_hold, info)

    unconfirmed = 0
    for attempt in range(1, HOLD_MAX_RETRIES + 1):
        await asyncio.sleep(HOLD_RETRY_SECONDS)

        try:
            info = await asyncio.to_thread(client.link_check, url)
        except PayPayLoginError:
            return "LOGIN"
        except PayPayError:
            continue

        if is_link_received_by_me(info.raw):
            return "RECEIVED"
        if is_link_dead(info.raw):
            return "GONE"
        order_id = order_id or info.order_id
        amount = amount or info.amount

        if is_link_pending(info.raw):
            if accept_sent and attempt % ACCEPT_RETRY_EVERY:
                continue
            result = await asyncio.to_thread(_try_receive, client, url, passcode, info)
            accept_sent = True
            if result in ("RECEIVED", "LOGIN", "FAILED"):
                return result
            continue

        if not accept_sent:
            return "GONE" if str(info.status).upper() in LINK_RECEIVED_STATES else "FAILED"

        try:
            history = await asyncio.to_thread(client.get_history, 20)
        except PayPayLoginError:
            return "LOGIN"
        except PayPayError:
            history = {}
        if order_id and amount and find_receive_in_history(history, order_id, amount):
            return "RECEIVED"
        unconfirmed += 1
        if unconfirmed >= UNCONFIRMED_LIMIT:
            return "FAILED"

    return "FAILED"
