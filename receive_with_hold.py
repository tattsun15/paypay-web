"""保留対処つきの受け取りフロー。自販機・代行Botなど、「受け取れたのかどうか分からない」
状態や、PayPay側の一時保留を正しく処理するためのお手本。

このフローが解く問題：
- link_receive の応答がタイムアウトすると、お金を受け取れたか失敗したか分からない
- PayPay側が「一時保留」（42007013）で受け取りを断ることがある。少し待つと受け取れる
- そのまま「失敗」にすると、本当は受け取れたお金を取り逃がす
- そのまま「成功」にすると、受け取れていないのに商品を渡してしまう
- → 保留にして、あとから確実に判定する

判定の順番（速くて確実な順）：
1. link_check の isReceiver を見る（1回の通信で済む。2026-10から入った新しい項目）
2. リンクがまだ受け取り待ちなら、1分ごとに受け取りをやり直す
3. 取引履歴から orderId + 金額 で探す（確実だが遅い）
4. リンクが CANCELED / EXPIRED になっていたら「GONE」
"""

import inspect
import logging
import time

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


def _call_on_hold(on_hold, info) -> None:
    if on_hold is None:
        return
    try:
        result = on_hold(info)
        if inspect.isawaitable(result):
            log.warning("on_hold に async 関数は使えません（receive_with_hold_async.py を使ってください）")
            result.close()
    except Exception as e:
        log.warning(f"on_hold でエラーが出ました（受け取りの確認は続けます）: {e!r}")


def receive_with_hold(client: PayPay, url: str, passcode: str | None = None, on_hold=None) -> str:
    """送金リンクを受け取る。保留になったら、確実な判定が出るまで最大40分繰り返す。

    on_hold：保留に入ったときに1回だけ呼ばれる。引数は LinkInfo（リンクを読めなかったときは None）。
      お客さんに「決済が保留中です」と出すのに使う。ここでエラーが出ても、受け取りの確認は続ける。

    戻り値：
      "RECEIVED"  成功（受け取り確認済み）
      "GONE"      リンクが別のアカウントに受け取られた・期限切れ・キャンセル（受け取っていない）
      "FAILED"    確認できなかった（人が確認）
      "LOGIN"     ログインが切れている（呼び出し側で入り直してからもう一度）
    """
    accept_sent = False
    order_id, amount = "", 0

    try:
        info = client.link_check(url)
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
        result = _try_receive(client, url, passcode, info)
        if result != "HOLD":
            return result
        accept_sent = True

    _call_on_hold(on_hold, info)

    unconfirmed = 0
    for attempt in range(1, HOLD_MAX_RETRIES + 1):
        time.sleep(HOLD_RETRY_SECONDS)

        try:
            info = client.link_check(url)
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
            result = _try_receive(client, url, passcode, info)
            accept_sent = True
            if result in ("RECEIVED", "LOGIN", "FAILED"):
                return result
            continue

        if not accept_sent:
            return "GONE" if str(info.status).upper() in LINK_RECEIVED_STATES else "FAILED"

        try:
            history = client.get_history(size=20)
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


if __name__ == "__main__":
    client = PayPay.load("session.json")
    if client is None:
        raise SystemExit("先に example.py でログインしてセッションを作ってください")
    client.ensure_alive(otp_input=input)

    url = input("受け取るリンク: ").strip()
    result = receive_with_hold(client, url, on_hold=lambda info: print("保留になりました。確認が取れるまで待ちます（最大40分）"))
    print({"RECEIVED": "受け取り成功", "GONE": "リンクが既に処理済み", "FAILED": "確認できず（要人力）", "LOGIN": "ログイン切れ"}[result])

    client.save("session.json")
