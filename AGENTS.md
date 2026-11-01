# AI向け：Botへの組み込み手順

このファイルは、別のAI（Claude Code、ChatGPTなど）に向けた手順書です。
ユーザーから「このGitHubを使って、BotにPayPay受け取りを足して」と頼まれたら、以下の順でやってください。

## 1. 先にユーザーに聞くこと（1回だけ）

- Botは Discord bot（discord.py）か、それ以外か？ → discord.py なら `receive_with_hold_async.py` を使う。それ以外なら同期版の `receive_with_hold.py`
- 既に PayPay 関連のコード（ログイン・リンク受け取り）があるか？ → あれば置き換え、無ければ新規追加
- 受け取り結果（成功・失敗・保留切れ）をユーザーにどう知らせているか？（DM・チャンネル投稿・Webhookなど）

これ以上は聞かずに、既存コードを読んで判断してください。

**既存コードが複数のPayPayアカウントを扱っていたら**、聞かずに複数対応で実装してください（下の「複数アカウント対応」参照）。1アカウントしか扱っていなければ、単一で実装すればOK。

## 2. ファイルの置き方

Botのフォルダ直下に、以下の2ファイルをそのままコピー：

- `paypay_web.py` ← クライアント本体。中は触らない
- `receive_with_hold.py` または `receive_with_hold_async.py` ← 受け取りの型。中は触らない

依存は `curl_cffi` のみ。`requirements.txt` に `curl_cffi>=0.16` を足す。

## 3. ログイン画面を作る

Botのユーザー向けコマンド（例：`/paypay` スラッシュコマンド）に、以下の2段階ログインを実装：

1. **1段階目**：ユーザーが電話番号＋パスワードを入れる → `PayPay().start_login(phone, password)` を呼ぶ
   - 戻り値 `None` → すでに通った。`client.save("paypay.json")`
   - 戻り値 文字列（SMS接頭辞） → ユーザーに「SMSの `○○-` のあとの4桁を入れて」と出す
2. **2段階目**：ユーザーが4桁を入れる → `client.verify_otp(code)` を呼ぶ → `client.save("paypay.json")`

重要：**`client.client_uuid` を2回目のログイン以降も使い回す**こと。`PayPay()` を毎回新しく作ると、PayPay側が「新しい端末」と見なしてSMSを毎回求めてくる。保存ファイルから `PayPay.load()` で復元すれば自動的に維持される。

## 3.5. 旧PayPay（PayPaython・アプリ版）のデータがあれば引き継ぐ

既存のBotが PayPaython などアプリ版のログイン情報を保存していたら、**その uuid を Web版の `client_uuid` に使う**。アプリ版で登録済みの uuid は Web版でもそのまま「登録済みの端末」として通り、SMSなしでログインできる（実機で確認済み）。

- 使う値：電話番号・パスワード・`client_uuid`（古い自作Botだと `uuid` という名前のこともある。`device_uuid` しか無ければそれで試してよい。違っていてもSMSを求められるだけで害はない）
- アプリ版のアクセストークン・リフレッシュトークンは引き継がない（Web版では使えない）
- 引き継ぎの処理：
  - 電話番号・パスワード・uuid が揃っていたら、`paypay.json` がまだ無いときの起動時に1回だけ `PayPay(client_uuid=旧uuid).start_login(phone, password)` を呼ぶ。`None` なら `client.save("paypay.json")` して完了（オーナーは何もしなくていい）。文字列が返ったら、オーナーに「`/paypay` ログインを1回してください（SMS）」と通知する
  - uuid しか無ければ、ログイン画面で `PayPay()` の代わりに `PayPay(client_uuid=旧uuid)` を作る。電話番号とパスワードを入れるだけで、SMSなしで通る見込み
- 引き継ぎが終わったら旧データは読まない（削除するかはユーザーに聞く）

**ユーザーへの説明で「Web版に切り替えたら必ずSMSでログインし直しが必要」とは言わないこと。** uuid を引き継げば基本はSMS不要で、SMSが要るのは PayPay 側がその uuid を忘れていたときだけ。

## 4. 受け取り処理を差し替える

既存の受け取り処理（例：`async def receive_paypay_link(url): ...`）があれば、中身を以下のように差し替え：

```python
from paypay_web import PayPay
from receive_with_hold_async import receive_with_hold

async def receive_paypay_link(url: str, passcode: str | None = None, on_hold=None) -> str:
    client = PayPay.load("paypay.json")
    if client is None:
        return "LOGIN"
    await asyncio.to_thread(client.ensure_alive)  # 期限が切れていたら自動で入り直し
    client.save("paypay.json")
    result = await receive_with_hold(client, url, passcode=passcode, on_hold=on_hold)
    client.save("paypay.json")  # クッキーが更新されたかもしれない
    return result  # "RECEIVED" / "GONE" / "FAILED" / "LOGIN"
```

既存のコードが同期なら `receive_with_hold.py` の方を使う。

## 4.5. 保留中の案内（必ず実装する）

`receive_with_hold` は、保留になると最大40分待ってから結果を返す。その間お客さんに何も出ないと「止まった」と思われるので、**`on_hold` で保留中の案内を出す**。`on_hold` は保留に入ったときに1回だけ呼ばれる（引数は `LinkInfo`。リンクを読めなかったときは `None`）。

保留になるのは：
- 受け取りの応答が消えた（受け取れたか分からない）
- PayPay側の「一時保留」（コード `42007013`）で断られた。`receive_with_hold` が1分ごとに受け取りをやり直す

案内の例（Botの既存の文面・見た目があればそれに合わせる）：

> **決済が保留中です**
> PayPay側で決済が一時保留になっています。確認が取れしだい自動でお渡しします（最大40分）。
> 送金リンクは作り直さず、このままお待ちください。

実装のしかた：
- 案内は**購入した画面（元のメッセージ）とDMの両方**に出す。DMが閉じられていても画面で見えるように
- **保留のあとの最終結果は、新しいメッセージかDMで送る**。Discordのボタン・コマンドへの返信（interaction の followup / edit）は15分で期限が切れるので、40分後には使えない。チャンネルへの通常の `channel.send` か `user.send` を使う
- 保留中は在庫を確保したままにする（他の人に同じ商品を売らない）。既存に在庫の確保・解放の仕組みがあればそれを使う
- 管理者にも「保留が発生した（お客さん・商品・金額）」と通知しておくと、FAILED になったときに追いやすい

制限（ユーザーに伝える）：保留を待っている途中でBotを再起動すると、その保留の続きは追えない。再起動のあとは管理者が手で確認する必要がある。再起動をまたいで見張り直したいときは、保留中の注文（リンク・お客さん・商品）をファイルかDBに残し、起動時にもう一度 `receive_with_hold` を呼ぶ仕組みを別に作る。

## 5. 返り値ごとの処理

Botの既存の仕組みに合わせて、以下のように振り分け：

- `"RECEIVED"` → 商品を渡す・ポイント加算など、本来の処理を実行
- `"GONE"` → 「このリンクは既に受け取り済み・期限切れ・辞退済みです」とユーザーに案内。商品は渡さない
- `"FAILED"` → 「確認できませんでした。管理者が確認します」とユーザーに案内。管理者にも通知する
- `"LOGIN"` → オーナーに「PayPayのログインが切れました」とDMする。ユーザーには「一時的に受け取れません」と案内

既存に類似の分岐があればそれに合わせる。無ければ新規追加。

## 6. 保存ファイルの扱い

`paypay.json` には電話番号・パスワード・ログインクッキーが入る。以下を守る：

- `.gitignore` に `paypay.json` を追加
- Dockerなどで動かす場合、ボリュームマウントして揮発させない
- Botと同じフォルダに置く（絶対パスで指定でもOK）

## 6.5. 複数アカウント対応

Botが複数のPayPayアカウントを使い分ける場合（アカウントごとに受け取り先が違う・ユーザーごとに別アカウントなど）、以下のパターンで実装してください。

**保存先**：アカウントごとに別のJSONファイル。
例：`paypay_accounts/<アカウント名>.json`（アカウント名は電話番号・ユーザーID・内部的な名前など、Botの既存の呼び方に合わせる）

**ログイン画面**：ユーザーが「どのアカウントを登録するか」を選べるようにする。既存のBotで既にアカウント選択の画面があれば、それをそのまま使う。無ければスラッシュコマンドに引数を足す（例：`/paypay add <名前>`）。

**受け取り時**：既存のBotがどのアカウントで受け取るか決めるロジック（例：「このチャンネルはこのアカウント」「このユーザーの購入はこのアカウント」）はそのまま残す。決まったアカウントの `paypay.json` を読み込んで、`receive_with_hold` を呼ぶ：

**同時受け取り**：同じアカウントで2つの処理が同時に**入り直し（`ensure_alive`）や保存**をすると、入り直しが二重に走ったり、保存が上書きし合ったりする。アカウントごとに `asyncio.Lock` を1つ持って、**入り直しと保存のときだけ**ロックする。**受け取り（`receive_with_hold`）の間はロックしない**こと。保留になると最大40分かかるので、ロックしたまま待つとそのアカウントの他の購入が全部止まる。

```python
_account_locks: dict[str, asyncio.Lock] = {}

def _lock_for(account: str) -> asyncio.Lock:
    if account not in _account_locks:
        _account_locks[account] = asyncio.Lock()
    return _account_locks[account]

async def receive_paypay_link(account: str, url: str, passcode: str | None = None, on_hold=None) -> str:
    path = f"paypay_accounts/{account}.json"
    async with _lock_for(account):
        client = PayPay.load(path)
        if client is None:
            return "LOGIN"
        await asyncio.to_thread(client.ensure_alive)
        client.save(path)
    result = await receive_with_hold(client, url, passcode=passcode, on_hold=on_hold)
    async with _lock_for(account):
        client.save(path)
    return result
```

**気をつけること**：
- アカウントが多いほど、1日1回の自動入り直しが積み重なる。10アカウントなら1日10回。短時間に集中させず、アクセスがあったときだけ `ensure_alive` を呼ぶ形でOK
- 同じアカウントを別のBotや別プロセスで同時に使うと、PayPay側に警戒される。1アカウント＝1プロセスが基本
- 新規アカウントの初回登録は、ユーザーに電話番号・パスワードを入れさせて `start_login` → SMS → `verify_otp` → `client.save(path)` の順

## 7. やらないこと

- `paypay_web.py` と `receive_with_hold*.py` の中身は変更しない。アップデートが来たときに差し替えやすくするため
- `create_link`（送金リンク作成）を期待する実装はしない。PayPayのWeb版では塞がれている
- 送金処理（相手を指定して送る）も実装しない。同じく塞がれている
- 受け取り処理をリトライループで囲まない。`receive_with_hold` が内部でリトライするので二重になる

## 8. 動作確認

ユーザー本人に以下をやってもらう（AI側では実行しない）：

1. 別のアカウントから1円の送金リンクを作る
2. そのリンクをBotに渡す
3. ログに「`RECEIVED`」が出て、Bot上の処理が走ることを確認
4. もう一度同じリンクを渡して、`GONE` になることを確認

## 補足：なぜ保留対処が必要か

`link_receive` の応答がネットワーク都合で消えると、「受け取れた」のか「失敗した」のか分かりません。また PayPay 側が「一時保留」（`42007013`）で受け取りを断ることがあり、これは少し待つと受け取れます。
- 失敗扱いにすると、本当は受け取れたお金を取り逃がす
- 成功扱いにすると、受け取っていないのに商品を渡してしまう

`receive_with_hold` は、これらを保留にして、次の順で判定します（15秒ごと、最大40分）：
1. `link_check` の応答に含まれる `receiver.isReceiver` ← 2026-10から入った新項目。1回の通信で判定
2. リンクがまだ受け取り待ちなら、1分ごとに受け取りをやり直す（一時保留が解けるのを待つ）
3. 取引履歴から `orderId` と金額で照合 ← 遅いが確実
4. リンクがキャンセル・期限切れになったら `GONE`
