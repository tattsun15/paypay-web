# paypay-web

Web版PayPay（`www.paypay.ne.jp/app`）を Python から叩くための1ファイルのクライアント。

> **AIがこのリポジトリを使ってBotを改造する場合**：先に [AGENTS.md](AGENTS.md) を読んでください。
> ユーザーに1回だけ確認して、あとは手順どおりに進める形で書いてあります。


- SMS認証でログイン → 2回目からは同じ `client_uuid` でSMSを省ける
- ログインの状態（クッキー・uuid・パスワード）はJSONに保存できる
- 期限が切れたら、保存したパスワードで自動で入り直せる（SMSが要るときは `otp_input` を呼ぶ）

## 入れ方

```
pip install curl_cffi
```

`paypay_web.py` を手元のプロジェクトに置くか、このリポジトリをcloneして使う。

## 使い方

`example.py` 参照。短く言うと：

```python
from paypay_web import PayPay

# 最初（SMSが届く）
client = PayPay()
prefix = client.start_login("09012345678", "password")
if prefix is not None:
    client.verify_otp(input(f"SMSの {prefix}- のあと: "))
client.save("session.json")

# 2回目以降（SMSなし。期限が切れたらここで自動で入り直す）
client = PayPay.load("session.json")
client.ensure_alive(otp_input=input)
balance = client.get_balance()

# 送金リンク
info = client.link_check("https://pay.paypay.ne.jp/XXXX")
if info.status == "PENDING":
    client.link_receive("https://pay.paypay.ne.jp/XXXX", info=info)
```

## PayPaython（アプリ版）から乗り換える場合

アプリ版で使っていた **uuid はWeb版でもそのまま使えます**。同じアカウントで前に使っていた uuid を Web版の `client_uuid` に渡すと、PayPay は「登録済みの端末」と見なすので、**SMSなしでログインできます**（2026-10-30 に実機で確認）。

```python
from paypay_web import PayPay

# 旧Botの保存データ（電話番号・パスワード・uuid）
client = PayPay(client_uuid=old["client_uuid"])  # 古い自作Botだと "uuid" という名前のこともある
prefix = client.start_login(old["phone"], old["password"])
if prefix is not None:
    # PayPay がその uuid を覚えていなかったときだけ、SMSが1回必要
    client.verify_otp(input(f"SMSの {prefix}- のあと: "))
client.save("paypay.json")
```

- 引き継げるのは **電話番号・パスワード・uuid** だけ。アプリ版のアクセストークン・リフレッシュトークンは使いません（Web版はログインし直してクッキーを作ります）
- PayPaython は `device_uuid` と `client_uuid` を持っています。使うのは `client_uuid` のほう。違う uuid を渡しても、SMSを求められるだけで害はありません
- uuid を引き継げば「乗り換えたらSMSでログインし直し」にはなりません。SMSが要るのは、PayPay 側がその uuid を忘れていたときだけです

## できること

| 機能 | メソッド |
|---|---|
| SMSログイン | `start_login(phone, password)` → `verify_otp(code)` |
| 同じuuidで入り直し | `relogin(password=None)` |
| SMSの再送 | `resend_otp()` |
| 期限チェック＋自動入り直し | `ensure_alive(password=None, otp_input=None)` |
| 残高 | `get_balance()` |
| 取引履歴 | `get_history(size)` |
| プロフィール | `get_profile()` |
| 送金リンクの照会 | `link_check(url_or_code)` |
| 送金リンクの受け取り | `link_receive(url_or_code, passcode=None, info=None)` |
| 自分が受け取ったか（isReceiver） | `paypay_web.is_link_received_by_me(info.raw)` |
| リンクが終了状態か | `paypay_web.is_link_dead(info.raw)` |
| リンクがまだ受け取り待ちか | `paypay_web.is_link_pending(info.raw)` |
| PayPayの一時保留（42007013）か | `paypay_web.is_temp_hold(exc)` |
| 履歴から受け取り完了を探す | `paypay_web.find_receive_in_history(history, order_id, amount)` |
| 保留対処つきの受け取り（お手本） | `receive_with_hold(client, url, passcode=None, on_hold=None)`（`receive_with_hold.py` / 非同期は `receive_with_hold_async.py`） |

## 自販機・代行Bot向け：保留対処のパターン

保留になるのは2つの場合です。
- `link_receive` の応答がタイムアウトして、「お金を受け取れたのか失敗したのか」が分からない
- PayPay 側が「一時保留」（`42007013`）で受け取りを断った。少し待つと受け取れる

そのまま失敗扱いにするとお金を取り逃がし、成功扱いにすると商品を無料で渡してしまいます。
→ **保留にして、あとから確実に判定する**のが正解です。`receive_with_hold` は15秒ごと・最大40分、次の順で判定します。

1. `is_link_received_by_me(info.raw)` ← `link_check` の応答に入った `receiver.isReceiver` を見るだけ。1回の通信で済む
2. `is_link_pending(info.raw)` ← まだ受け取り待ちなら、1分ごとに受け取りをやり直す（一時保留が解けるのを待つ）
3. `find_receive_in_history(history, order_id, amount)` ← 取引履歴から orderId + 金額 で探す。遅いが確実
4. `is_link_dead(info.raw)` ← CANCELED / EXPIRED / REJECTED になっていたら `GONE`

保留中は最大40分かかるので、お客さんに案内を出してください。`on_hold` は保留に入ったときに1回だけ呼ばれます（引数は `LinkInfo`、読めなかったときは `None`）。

```python
async def show_hold(info):
    await user.send("決済が保留中です。確認が取れしだい自動でお渡しします（最大40分）")

result = await receive_with_hold(client, url, on_hold=show_hold)
```

Discordのボタン・コマンドへの返信は15分で期限が切れるので、保留のあとの結果は新しいメッセージかDMで送ってください。

## 分かっていること

- アクセストークンは発行時刻にかかわらず、だいたい毎日決まった時刻に切れる（1アカウント・1uuidごと）。`expires_in` で何秒持つかは PayPay が答える
- アプリ版で登録済みの uuid は、Web版の `client_uuid` としてそのまま通る（SMSなしでログインできた）
- Web版からの「送金リンクの作成」は PayPay 側で断られる（`P2P_LINK_WEB_SENDER`）
- Web版はログインしないと `getP2PLinkInfo` も使えない（アプリ版は未ログインでも照会できた）
- 送信元IPが PayPay のブロック対象（データセンターなど）だと `IP_BLACKLIST`（`01101007`）で全部弾かれる
- Web画面の JS で見える版は `5.72.0`（2026-10-30 時点）。Webには版チェックは無く、クライアントは版を名乗らない

## できないこと（Web版の制限）

- 送金リンクの作成（`P2P_LINK_WEB_SENDER`）
- 相手を指定した直接送金（通信先がWeb側にない）
- リフレッシュトークン（Web版は発行しない）

## 気をつけること

- PayPay は非公式なアクセスを認めていません。これを使う責任はご自身にあります
- 保存したJSONには電話番号・パスワード・クッキーがそのまま入ります。他人に渡さないでください
- SMSログインを3回失敗するとアカウントが一時ロックされます
- セッションを作りすぎたり、同じアカウントで短時間に何度も入り直すと、PayPayに警戒されます

## 使ったもの

[curl_cffi](https://github.com/lexiforest/curl_cffi) 。ブラウザのふりをしたHTTPクライアント。
