# 優待クロス 講座 ＆ 自動検出ツール

株主優待クロス取引（つなぎ売り）を、ゼロから学んで自分で回せるようになるための **講座** と、
「どの銘柄を・いつ・どの方法でクロスし・いつ現渡しするか」を試算する **自動検出ツール** です。

- マンガ：[docs/manga/yutai_cross_manga.html](docs/manga/yutai_cross_manga.html)（全8話。まず全体像をつかむ）
- 講座：[docs/course/README.md](docs/course/README.md)（全10回＋チェックリスト）
- ツール：`yutai_cross/`（Python 3.9+、追加ライブラリ不要）

> **免責事項**：本リポジトリの内容とツールの出力は、作成者が個人的に調べた情報と試算であり、正確性・完全性を保証しません。特定の銘柄・証券会社・取引を推奨・勧誘するものではなく、投資助言ではありません。利用により生じた損失について責任を負いません。記載の証券会社や Yahoo 等とは関係がありません。料率・ルールは2026年9月時点の調査に基づく初期値です。発注前に必ず証券会社・会社IRで最新情報を確認し、投資の最終判断はご自身で行ってください。

## はじめての人の最短手順（10分）

1. **Python を入れる**
   - Windows：[python.org](https://www.python.org/downloads/) からインストール（インストール画面で「Add python.exe to PATH」にチェック）。以降のコマンドは `py -m yutai_cross …` と打つ
   - Mac：ターミナルで `python3 --version` と打つ。無ければ表示される案内に従ってインストール。以降は `python3 -m yutai_cross …` と打つ
   - このページや講座の `python -m …` は、Windows なら `py -m …`、Mac なら `python3 -m …` に読み替えてください
2. **このリポジトリを取得する**：GitHub の「Code」→「Download ZIP」でダウンロードして展開（git が使えるなら `git clone` でも可）
3. **README.md があるフォルダでターミナルを開く**
   - Windows：エクスプローラーでそのフォルダを開き、上のアドレス欄に `cmd` と入力して Enter
   - Mac：ターミナルで `cd `（cd と半角スペース）と打ってから、Finder でそのフォルダをターミナルにドラッグして Enter
4. `python -m yutai_cross init` → `python -m yutai_cross plan`（架空銘柄の表が出れば成功）
5. `mydata` フォルダの `watchlist.csv` を Excel で開き、架空の行を消して自分の銘柄を書く。保存は「CSV UTF-8（コンマ区切り）」で。書けたら `python -m yutai_cross check` で入力ミスがないか確認
6. 毎晩 `python -m yutai_cross today` を実行し、「次の営業日」に書かれたことをする
7. クロスしたら `python -m yutai_cross position add 銘柄コード --method sbi_short`（方法は today の表示どおり）で記録。現渡ししたら `position close`、優待が届いたら `position received` で記録

「No module named yutai_cross」と出たら、README.md があるフォルダにいません（手順3へ）。

## コマンド一覧

```bash
python -m yutai_cross init                 # mydata/ にサンプルのウォッチリストと設定をコピー
python -m yutai_cross check                # ウォッチリストと設定の入力チェック
python -m yutai_cross plan                 # 条件を満たす候補とスケジュールを表示
python -m yutai_cross plan --budget 1000000    # 資金枠100万円に収まる組み合わせだけ選ぶ
python -m yutai_cross plan -o mydata/reports   # Markdown / CSV / カレンダー(ics) も出力
python -m yutai_cross today                # 今日と次の営業日にやること・要対応の建玉
python -m yutai_cross position add X001 --method rakuten_short --price 1850   # クロスしたら記録
python -m yutai_cross position close 1     # 現渡ししたら記録（1 は建玉のID）
python -m yutai_cross position received 1 --value 3000   # 優待が届いたら記録（実績CSVに追記）
python -m yutai_cross position list        # 建玉の一覧
python -m yutai_cross calendar             # ウォッチリストの1年分の権利日カレンダー
python -m yutai_cross dates 2026-12-31     # 権利付最終日・権利落ち日・短期初日
python -m yutai_cross cost --record 2026-12-31 --price 2400 --benefit 1000   # コスト試算
python -m yutai_cross tax --year 2026      # 確定申告用メモ（優待の雑所得の集計）
```

`mydata/` はあなた専用のフォルダで、git の管理対象外です（公開リポジトリに載りません）。ウォッチリストの列の説明は [第10回](docs/course/10_ツールの使い方.md) にあります。

## 出力例

`python -m yutai_cross plan -w examples/watchlist_sample.csv --today 2026-10-06 --horizon 100`（銘柄はすべて架空）：

```
■ 次の営業日 2026-10-07(水) にやること（寄付で約定させる注文は今夜〜当日8:59までに）
  - [短期初日] X001 （架空）さくらフーズ: SBI証券 一般信用(短期) で建てられる初日。人気銘柄は前夜〜寄付前に在庫がなくなりやすい（参考。目安は 2026-10-16(金) の 楽天証券 一般信用(短期)）
  - [短期初日] X007 （架空）ほしぞらレジャー: SBI証券 一般信用(短期) で建てられる初日。人気銘柄は前夜〜寄付前に在庫がなくなりやすい（参考。目安は 2026-10-14(水) の SBI証券 一般信用(無期限)）

■ 条件を満たす候補（見込み利益の大きい順・試算）
コード 銘柄                 クロス日   どこで     付最終日      利益 状態
------ -------------------- ---------- ---------- ---------- ------- ------------------
X006   （架空）こもれび化粧 12/16(水)  楽天 短期  12/28(月)    2,770 あと48営業日
X001   （架空）さくらフーズ 10/16(金)  楽天 短期  10/28(水)    2,724 あと7営業日
X007   （架空）ほしぞらレジ 10/14(水)  SBI 無期限 10/28(水)    1,811 あと5営業日
```

## ツールの判定内容

- 東証の営業日（祝日法による祝日・振替休日・国民の休日、年末年始休場）と T+2 受渡しから、権利付最終日・権利落ち日・一般信用（短期）の建て可能開始日を計算。短期の返済期限は、SBI証券の「15営業日」と楽天証券の「14日（暦日）」の数え方の違いも反映
- 証券会社ごとの売建方法（一般信用 短期／無期限、制度信用）について、貸株料（受渡日ベースの日数）・逆日歩（想定／最高料率×4倍での見積もり）・手数料・配当落調整金と源泉税のズレを計算
- 損益分岐日（これより前に建てると最低利益を割る日）と、銘柄の人気度に応じたエントリー目安日を算出
- 長期保有条件・除外銘柄（勤務先など）・逆日歩リスク・連休による日数増・入力の読み違いなどを警告
- 目安どおりに建てた場合の必要資金ピーク（信用口座の最低保証金30万円を含む）を計算し、資金枠（`--budget`）を超える分は利回りの低い候補から見送る
- クロスした建玉を記録すると、現渡し日・返済期日・優待の到着確認を知らせ、優待が届いたら実績CSVに追記する
- 「人気」が空欄の銘柄は自動判定（3月・9月の権利、または優待利回り1%以上なら「高」）。料率の確認日が90日より古いと警告

在庫の確認と発注は、各証券会社の画面で行ってください（ログインが必要なため自動化していません）。

## テスト

```bash
python -m unittest discover -s tests -t .
```
