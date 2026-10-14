# yataverse lake 復元保証付き保存の研究開発 — 2026-10-11

既存 lake 全体を対象に、保存量・復元・計算費用・障害ドメインを評価する。
このディレクトリは実行可能な研究試作であり、本番の writer / reader / custody / R2 / B2 / Filecoin 設定は変更しない。
現段階では「LLM / Vector DB が既存方式を上回った」という結論は得られていない。
サンプル比較では **solid zstd + 消失訂正**が最良だった。凍結epoch6の全825,390ブロック / 91,395,086,102 bytesも実読・CID検証・圧縮・断片復元を完了した。全量ではzstd基準を測定し、全方式の最適性までは比較していない。

## 調査と実装

| 経路 | 実装内容 | 検証の位置付け |
|---|---|---|
| plain | ファイル単位の DEFLATE level 9 / raw 選択 | 通常圧縮の対照 |
| cdc | Gear CDC (2–16 KiB) + SHA-256 重複排除 | FastCDC の完全実装ではない |
| vector | CDC + 64次元のバイト頻度スケッチ + cosine top-4 + DEFLATE 辞書 | 語彙・構文の類似検索の対照。学習済み意味埋め込みではない |
| solid-zstd | 生ファイル群と復元メタデータを束ねて zstd level 9 | 強い通常圧縮の対照 |
| adaptive | 上記4方式の実バイト数を比較し最小を採用 | 全方式を試すため計算費用が大きい |
| LLM entropy coding | GPT-2 124M + 32bit算術符号化、版固定モデル | 実lakeのバイナリ・UTF8各4KiBで測定、別プロセス復号も検証 |

既存のSCI試作に加え、全量評価は Node 26 のネイティブ処理、実LLMは Python/PyTorch を使う。
全ソースの拡張子は `.cljk`。`cljk-origin.edn` に `.cljs` / `.js` / `.py` の元の言語を記録し、対応するランタイムで実行する。
研究用 `nbb.edn` はサービスの依存解決から独立している。
符号化したバイナリを JSON/base64 のまま保存せず、12-byte header + JSON metadata + binary bodies のコンテナにする。
ベクトル索引は符号化時だけ使う再構築可能な補助情報で、復号時には不要。
辞書参照は同じパック内の独立アンカーに限定し、参照チェーンを禁止する。
圧縮候補が外れても保存量への影響に留め、必ず元のバイトを復元する。

## 復元契約

GF(256), 多項式 `0x11d` の systematic (6,4) 符号を実装した。
`P = D0 xor D1 xor D2 xor D3`, `Q = D0 xor 2D1 xor 4D2 xor 8D3`。
**信頼済みマニフェストと任意の4個の正しい断片があれば、パックを完全復元できる。**
断片のサイズとSHA-256を検証し、破損断片は消失として扱う。3断片が欠損・破損した場合は拒否する。
復号後に各アンカー、各ファイル、パックの長さとダイジェストを照合する。
マニフェストは3コピー（断片位置0,2,4）とし、任意の2位置消失で少なくとも1コピー残る。
マニフェストの信頼ダイジェストは呼出元から与える。SHA-256 は署名の代用ではない。

この契約は誤りのない符号化・復号実装、SHA-256の安全性、必要な復号ソフトの存在を前提とする。
初期のSCI保存器は実験用。後述のWAN試験では、専用コピーのファイル・ディレクトリをfsyncし、実リンク経由の修復・読戻しを実施した。
電源断試験、WAL、常時修復デーモンはまだ実施していない。全lakeの実WAN転送は後述の永続custody試験で実施し、各工程の完了証拠を区別する。
したがって、数学的な断片復元とローカル試験は、運用時の耐久性・SLAの保証とは区別する。

`validate-placement` は拠点ごとの断片数も検査する。
6拠点に1断片ずつなら任意の2拠点消失、3拠点に2断片ずつなら任意の1拠点消失に対応する。
2 WAN に3断片ずつ置く構成は、1 WAN 消失で3断片失うため拒否する。
2拠点しか使えずどちらかの拠点消失から完全復元したい場合、各拠点に全情報を持たせる必要があり、
非圧縮情報に対する物理容量の下限は2倍になる（その後の保存中の変化・追加故障は別）。

## lake の全インベントリと実バイト測定

2026-10-07 epoch 6 のローカル凍結インベントリ `log-resolved.jsonl` を評価時に読み直した。
SHA-256: `2334fb3efefc460a9ed1301aecfa2b5183133fc0bb0208a93692617672eb8a91`。
825,390 unique CID / 91,395,086,102 bytes（約91.4 GB）。初期インベントリ走査ではメタデータだけを読んだが、後述の全量実バイト評価と永続custodyでは全ブロックを読み、CIDとサイズを検証した。
初期研究CLIは最新inga headと署名を再検証しなかった。後段のepoch6 head確認は5 witnessで実施し、凍結インベントリの評価と継続的な最新head追従を区別する。

| ブロックサイズ | 件数 | bytes |
|---|---:|---:|
| <1 KiB | 270,696 | 64,556,546 |
| 1–16 KiB | 37,191 | 117,799,717 |
| 16–256 KiB | 465,034 | 47,498,091,569 |
| 256 KiB–1 MiB | 51,296 | 24,769,687,292 |
| 1–8 MiB | 1,101 | 5,789,978,527 |
| ≥8 MiB | 72 | 13,154,972,451 |

CID hash rank とサイズ階層で48候補を選び、上限1 MiB/block、総読み出し上限8 MiBで測定。
18ブロック / 3,767,085 bytes をローカル raw store / Kubo flatfs から読み、サイズと元のCIDを検証した。
14候補はこのローカルコピーから読めず、16候補はサイズ・バイト上限で除外した。
読めた集合は偏りがある。**これを全 lake の圧縮率に外挿しない。**

| 18ブロックを束ねた経路 | パック bytes | EC・メタデータ込み bytes | 元データに対する割合 |
|---|---:|---:|---:|
| plain | 385,931 | 581,109 | 15.43% |
| cdc | 417,323 | 628,200 | 16.68% |
| vector | 402,346 | 605,736 | 16.08% |
| solid-zstd | 201,251 | 304,089 | 8.07% |
| adaptive（solid-zstd選択） | 201,251 | 304,089 | 8.07% |

物理bytesは6つのパディング済み断片、3マニフェスト、3レシートを含む。
一時ベクトルRAM、ファイルシステムの割当単位、保守用コピー、索引、暗号化、ログ、実運用の容量余裕は別。
各経路で全15通りの2断片消失からバイト一致を検証。
さらに別プロセスから元ブロックもネットワークも使わず4つの断片ファイルだけで18ファイルのSHAを照合した。
記録は `results/lake-summary.json`, `results/independent-restore.json`。

分割全量評価CLIも実lakeの行0–15で試験した。16/16ブロック、4,194,528 bytesを欠損なくCID検証し、
solid-zstdのEC・メタデータ込み容量は2,410,305 bytes（57.46%）だった。
こちらでも全15通りの2断片消失を各方式で検証した。`next-row=16`, `range-byte-coverage=true`,
`full-byte-coverage=false` と区別して記録する。`results/lake-range-summary.json` を参照。
8.07%の集合と57.46%の集合の差も、全量の実バイト評価が必要な理由になる。

合成の世代バックアップでも、vector は通常のファイル単位圧縮を上回ったが、solid-zstd がさらに小さかった。
ランダム・圧縮済み入力では圧縮利得がなく、EC込み約1.54倍になった。
そのため「常にAIを使う」方式は採らず、データごとの実測で選択する。
再現結果は `results/controls.json`。

## 比較費用

圧縮と保存サービスの価格を分ける。同じ圧縮パックをR2/B2/Filecoinに保存する対照も必要。
サービスの内部冗長化は公表単価に含まれるため、R2/B2の請求bytesを単純に3倍しない。

2026-10-11に公式ページを確認した容量単価：
- [R2 Standard](https://developers.cloudflare.com/r2/pricing/): $0.015 / GB-month。操作課金、無料枠、請求単位丸めを別扱い。
- [B2](https://www.backblaze.com/cloud-storage/pricing): 開始価格 $6.95 / TB-month。無料egressは保存量の3倍まで、transactions無料。
- [Filecoin公式のサービス一覧](https://www.filecoin.io/store-data)は複数サービスを掲載する。Filecoin全体に単一の価格・復元SLAを設定しない。
  一覧のFil One開始価格は$4.99/TBだが、[Fil One本体](https://www.fil.one/)の現行FAQは **$5.99/TB-month、最低$5.99/月** と明記しているため本体を優先する。
  通常用途のAPI/egressは無料（reasonable use）。91.4GBだけでも専用アカウントの最低料金は$5.99/月になる。
  Onchain Cloudの一覧は$2.50/TB・最低2コピーと表示するが、コピー単価・追加取得費・SLAを確定する見積もりは未取得。

この91.4 GBを未圧縮で保管した容量課金だけの概算はR2約$1.37/月、B2約$0.64/月（無料枠等を除く）。
小さな絶対額なので、自前ノードを増やす費用やLLMのGPU計算費用が節約額を超える可能性がある。
`storage_cost.cljk` は仮定を明示して損益分岐点を計算する。

`C_yata = logical_GB × measured_physical_ratio × node_cost_per_GB_month + other_costs`

`other_costs` にハード償却、電力、モデル・索引、符号化、取得・修復帯域、運用、保証水準の費用を入れる。
未知の自前単価をゼロとみなさず、Filecoinの料金と耐久性・取得性能も固定値で推測しない。
現時点で3サービスより総費用・性能が優れているとは証明していない。

## 実LLM測定と独立復元

`llm_codec.cljk` は公開 GPT-2 124M (`openai-community/gpt2`, revision
`607a30d783dfa663caf39e06633721c8d4cfcd7e`) の次トークン確率を量子化し、32bit算術符号化する。
CPU FP32 / softmax FP64、2 threads、256 tokensごとに文脈をリセットする。
バイナリは可逆 Latin-1→Unicode→GPT-2 tokenizer、UTF8は厳密なUTF8→tokenizerを使い、両方で元バイトを照合する。
モデルが予測を外してもバイト復元は失敗しない。ただし確率の数値計算が変わると復号できないため、
モデル6資産のSHA・版・量子化CDF全列のSHA・パック外部信頼SHAを検証する。

| 実lake入力 | 元 bytes | LLM本体 bytes | LLMコンテナ bytes | DEFLATE bytes | zstd9 bytes | 符号化秒 | 復号秒 |
|---|---:|---:|---:|---:|---:|---:|---:|
| row 0 先頭4KiB、バイナリ | 4,096 | 6,240 | 7,471 | 2,853 | 2,810 | 50.95 | 50.68 |
| row 121 先頭4KiB、厳密UTF8 | 4,096 | 1,335 | 2,564 | 1,355 | 1,341 | 16.46 | 16.41 |

モデル・tokenizer資産は **550,959,737 bytes**。上表のコンテナ容量に含まれず、復元保証にはその保管と復号ランタイムの維持が必要。
テキストでは本体だけならDEFLATEより20 bytes・zstd9より6 bytes小さいが、コンテナ込みでは大きい。この小さいサンプルだけで他のモデルや全lakeを一般化しない。
同一モデル・数値ランタイムを固定した `llm_restore.cljk` は新しいプロセスで元データ・ネットワークを使わず、
バイナリ4,096 bytesを50.66秒でSHA一致に復元した。別ハードウェア・別数値ランタイムでの移植性は未資格化。
結果は `results/llm-gpt2.json`, `llm-gpt2-utf8.json`, `llm-independent-restore.json`。
実LLMパック2個も全15通りの2断片消失からパックの全バイト一致を検証した（計30通り）。`results/llm-comparison.json`。
今回の測定ではLLM経路を既定保存方式に採用しない。

## 実リンクWAN障害注入・修復

`wan_trial.cljk` はXavier/Jacobの専用研究ディレクトリだけを扱う。Mithrilのリソースは扱わない。
各拠点に全6断片・3マニフェスト・3レシートを置く。片拠点の全データを使わず、残る拠点でも断片0/1と対応する主マニフェストを使わない。
SSH経由で残る4断片とバックアップマニフェストを読み、元lakeを読まず復元する。
その4断片から欠損側の新規専用ディレクトリへ全断片・メタデータを再生成し、fsync後に全断片・全マニフェスト・全レシートを読み戻す。

実パックの非圧縮コンテナは16,526,345 bytes、圧縮本体4,908,310 bytes。
1拠点の保存ファイル13個は7,364,818 bytes（ディスク割当7,401,472 bytes）。2拠点の実割当は14,802,944 bytes。
この容量にはブートストラップ用raw-trust.jsonも含む。修復用コピーは別の一時容量として残している。
片拠点のみ・4断片の取得は約2.54–3.36秒、復号は約0.04秒。
欠損側への7.36MB修復書込みは約4.42–6.72秒、その後全断片読戻しは約2.93–3.99秒（メタデータ読戻し時間は別）。

これは **実リンク上でクライアントが欠損拠点を使わない注入試験** で、物理WANを停止していない。
2026-10-11のユーザー回答は「一部を共有、または独立性は未確認」。2台の物理ノードの保存と片ノードからの復旧を評価するが、独立したWAN・ルーター・電源を持つ2障害ドメインとしては資格化しない。外部IPのtrace確認は自動承認審査で拒否されたため実施しなかった。
キャッシュを強制破棄していない。全lakeのWAN復元時間へ線形外挿しない。`results/wan-trial.json` に数値と前提を保存。

R2/B2の[公称11 nines](https://developers.cloudflare.com/r2/reference/durability/)は[年間耐久性の設計値](https://help.backblaze.com/hc/en-us/articles/218485257-B2-Resiliency-Durability-and-Availability)。
有限回の消失訂正試験はこの値や契約SLAと同等の保証を証明しない。Filecoinも契約・サービスごとの条件を読む必要がある。
[Onchain Cloud](https://filecoin.cloud/)は2独立プロバイダ・24時間ごとのPDPを示すが、自動修復はロードマップに置かれている。
同等性の判定には、復元バイト一致のほか、RPO、故障相関、修復猶予と帯域、scrub、メタデータ/鍵/モデルのcustody、保守・契約条件が必要。

## 全量実バイト評価器

`lake_stream.cljk` は全825,390行を固定SHAで照合し、読めた各ブロックの全バイトをCID検証する。
最大256MBのブロックも欠損扱いで除外せず、4MiB窓を16MiB以下のパックへまとめる。
各パックをzstd level9で圧縮し、実際に6断片へ符号化し、断片0/1を欠損させて復元・展開し、非圧縮コンテナとバイト一致を確認する。
物理容量はパディング済み全6断片と3マニフェスト・3レシートを計上する。2WANで片拠点消失＋残る2断片消失に耐える構成は、その2倍。
同じ圧縮パックをR2/B2/Filecoinへ保存する対照には圧縮本体のbytesを課金容量として使う。サービス内部のEC容量を追加請求しない。

5,000行ごとに `progress.json` とfsync済み監査台帳を保存する。欠損がある全行走査はexit2とし、`complete=true` と `fullByteCoverage=true` を区別する。
信頼SHAを渡した全行台帳の `cid-verified` 行だけを `--exclude-ledger` で除外して、別custodianが欠損集合を補完できる。
`merge_evaluations.cljk` は両台帳の全行について同じrow/CID/bytesと、ちょうど1回の実バイト評価を照合する。
coverageが揃わなければ結果を拒否する。パック境界はcustodyによる分割を記録し、読めた集合だけを全量と呼ばない。
全量圧縮測定は一時メモリ内で実施し、最初のWAN試験パック以外を恒久保存しない。保存基盤の本番移行完了を意味しない。

ローカルの全行走査は825,390行で完了し、479,053ブロック / 58,315,106,160 bytesを実読・CID検証・断片復元できた。
346,337ブロック / 33,079,979,942 bytesはそのcustodyから読めなかった。`results/local-custody-census.json`。
読めた全バイトのzstdパックは15,545,057,420 bytes、1 ECの断片・メタデータ込みは23,325,563,445 bytes。
欠損集合33,079,979,942 bytesはXavierで全量補完し、未読ゼロで完了した。二つの全行台帳をCID・行番号・サイズ・状態・SHAで照合し、各行が正確に一度実検証されたことを確認した。
この長いローカル実行の途中で作業ソースを編集したため、終了時の作業ファイルSHAと実行コードSHAが異なった。
元の未加工報告を保持し、開始前にXavierへコピー済みの同一ソースのSHA
`d0db498d84743b6dc59a3ce63708955cb6d0ae5fca827390933e8545c2f73d99`をruntime provenanceとして明示した。
次の実行ではソースSHAを開始時に固定し、別名の実行用コピーを編集しない。

### 凍結epoch6全量の確定結果

`results/full-e6-union.json` と `results/full-e6-costs.json` に全行照合と容量費用を保存した。GBは10^9 bytes。

| 項目 | 実測bytes | GB |
|---|---:|---:|
| lake元バイト、825,390ブロック | 91,395,086,102 | 91.395 |
| 復元メタデータ込み非圧縮パック、5,620個 | 91,536,624,664 | 91.537 |
| zstd圧縮パック（サービスへ置く対照） | 20,938,877,542 | 20.939 |
| 1組の(6,4) EC、断片・3コピーのmanifest/receipt | 31,420,792,722 | 31.421 |
| 2拠点に完全なECを各1組 | 62,841,585,444 | 62.842 |

圧縮パックは元バイトの22.91%（77.09%削減）、2拠点ECは68.76%（31.24%削減）。
各ブロックはCID検証後にパックへ入れ、2断片消失から圧縮パックを復元・展開し、元コンテナの全バイトと照合した。
試験中の実行ホスト時間の合計は約34.92分、圧縮約21.19分、EC符号化約1.67分、復元・展開約3.74分。
それぞれの処理時間を含むホスト実行時間であり、全量WAN復元時間・サービス取得速度・課金CPU時間ではない。
macOSの単一zstd workerとLinuxの3 workersによるcustody別パックで、同一ホストの連続スループット比較ではない。

| 保存先 | 元91.395GBの容量料金/月 | 同じ圧縮20.939GBの容量料金/月 |
|---|---:|---:|
| R2 Standard | $1.371 | $0.314 |
| B2 | $0.635 | $0.146 |
| Filecoin上のFil One専用サービス | $5.99 | $5.99（最低月額） |
| 自前2拠点EC | — | 62.842GB × 実ノード単価、運用費は別 |

容量単価だけの限界費用であり、無料枠・請求丸め・操作・移行計算・取得修復・運用・保証費用は含まない。
自前保存が同じ圧縮を置くR2/B2を上回るには、他費用を無視してもGB-month単価がそれぞれ
$0.004998未満 / $0.002316未満である必要がある。実運用費を加えると条件は厳しくなる。
この段階の圧縮評価はファイルシステム割当、暗号化、グローバル索引、全量の永続保存・WAN復元を含まない。後述の永続custodyで別途実測する。常時修復は未実装。
よって全量の容量改善は確認できたが、同等耐久性での総費用・速度の優位性は未証明。

`results/live-head-check.json` は現在のheadを5 witnessでepoch6 / CID
`bafkreiezmplv33krtco5jmbxp6jepnnvpdse2k2bu7epslfrmxavjzmnty`として検証した記録。
凍結時のbundleと同じheadだった。ただし今回インベントリ自体を再resolveしたという意味ではない。

```sh
node stream_test.cljk /absolute/new/fixture
node lake_stream.cljk --inventory /absolute/frozen.jsonl --sha256 SHA --flatfs /absolute/blocks --raw /absolute/raw --out /absolute/new/run
node lake_stream.cljk --inventory /absolute/frozen.jsonl --sha256 SHA --flatfs /absolute/blocks --raw /absolute/raw --exclude-ledger /absolute/primary/blocks.jsonl --exclude-ledger-sha LEDGER_SHA --zstd-workers 3 --out /absolute/new/secondary
node merge_evaluations.cljk /absolute/primary /absolute/secondary /absolute/new/full.json
node compare_costs.cljk /absolute/full.json /absolute/new/cost.json /absolute/explicit-assumptions.json
python3 llm_codec.cljk --input /absolute/input.bin --out /absolute/new/llm --revision 607a30d783dfa663caf39e06633721c8d4cfcd7e
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 llm_restore.cljk --pack /absolute/pack.bin --trust PACK_SHA --model-root /absolute/snapshot --out /absolute/new/restore
node wan_trial.cljk /absolute/reviewed-two-site-config.json /absolute/new/wan-trial
```

外部サービスの現状: 非Mithrilの接続済みCloudflareアカウントはR2未有効化、B2既存認証はunauthorized、Filecoinの対象契約・取得資格情報は未提示。
Mithrilのバケットを候補にした4KiB PUTは自動承認審査で拒否され、実行されていない。その後のユーザー指示によりMithrilをこのagentの対象から除外した。
したがって外部3サービスの実PUT/GET速度と実請求TCOは未測定であり、公表単価の試算と混同しない。

`compare_costs.cljk` は全量カバレッジを確認した報告だけを受け付ける。
同じ圧縮パックを各サービスへ置く容量対照と、自前2WANの約3倍の圧縮本体容量を並べる。
自前のディスク単価、固定運用費、取得・修復費、初期符号化の償却期間、ホスト時間単価を与えない限りTCOは `null`。
仮定を与えた値も測定済み請求額として扱わない。GPU/CPU時間、モデルの維持、電力、ハード償却、保守、索引、暗号化と鍵保管、空き容量余裕をゼロとみなさない。
メモリ内の全量試験は全ファイルの実ディスク割当量を測らないため、全量の物理bytesはECフォーマットの値であり、
WAN試験パックで測ったAPFS/ext4の割当量と区別する。サービス内部の物理bytesも非公開であり、請求bytesと同一とは断定しない。
保存容量だけの損益分岐点でも、自前2WAN ECのGB-month単価は、およそR2の1/3・B2の1/3以下が必要になる。
その上に実運用費と同等の保証を維持する費用を加える。

## 再現

repoの `research/` へ移動して実行。`--out`, `--report`, `--work-root`, `--shard-output` は新規パスを指定。
出力は `/Users/junkawasaki/github/workspaces/codex/<task>/` 以下に置く。
Node 26（組込みzstd）と .cljk 対応の kbb SCI engine を使用。

```sh
kbb --backend sci --classpath . storage_test.cljk
kbb --backend sci --classpath .:../deploy lake_test.cljk --work-root /path/to/new-fixture
kbb --backend sci --classpath . storage_bench.cljk \
  --source-ref f83775164f7de50321a69a39ff741137ab26b63a --out /path/to/new-run
kbb --backend sci --classpath . storage_cost.cljk --node-usd-gb-month 0.003 --other-usd-month 0

kbb --backend sci --classpath .:../deploy lake_evaluate.cljk \
  --inventory /path/to/frozen-inventory.jsonl --sha256 EXPECTED_SHA --count EXPECTED_ROWS \
  --flatfs-root /path/to/kubo/blocks --raw-block-store /path/to/raw-blocks \
  --limit 48 --max-block-bytes 1048576 --max-read-bytes 8388608 \
  --report /path/to/new-report.json --shard-output /path/to/new-shards

kbb --backend sci --classpath . storage_restore.cljk \
  --case /path/to/new-shards/solid-zstd --trust MANIFEST_SHA --lost 0,2
```

全量の分割評価は `lake_evaluate.cljk --all --start-row N --limit L`。
範囲に未読・予算除外があればレポートを残してexit 2、全量完了を主張しない。
各回最大16 MiBのバッチのみ比較する。大ブロックは全CIDを検証後に4 MiB窓で評価する。
CLIは最大256 MBの1ブロックを読み込む。後述のnative全量実装はパック単位のメモリ処理と監査台帳を使う。途中からの永続チェックポイント再開は次の実装課題。
flatfsのlayoutはmultihash/base32/next-to-last-2限定。未知のlayout・hash・サイズは拒否する。

## 次の資格化段階

1. 最新headの5-witness確認と凍結epoch6全バイト評価は完了。次はinventory再resolve、継続更新時の差分追跡、全量永続custodyと外部サービス実測を資格化する。
2. CID → pack → record の索引、ランダムread、更新、GC、修復トラフィックを測る。4–16 MiB packを出発点に、圧縮率と取得増幅のParetoを比較する。
3. 学習済み埋め込み + ANN とLLM entropy codecを通常圧縮と競わせる。モデルの容量、版、tokenizer、整数確率表、復号再現性とGPU時間を含める。LLMが外れても可逆性を維持する。
4. 対象CIDの元バイトを保つreaderアダプタを実装する。外部CIDを書き換えず、公開HTTP/IPLD契約と署名済みgraph headを維持する。
5. 独立拠点配置、永続化、暗号化・鍵復旧、署名済みmanifest、実ノード喪失・完全修復を試験してから移行候補を判断する。

圧縮・重複排除は暗号化前の許可された境界で行う。異なる利用者の秘密データを横断する重複判定は別の機密性設計が必要。
今回の取得対象は既存の公開lakeインベントリのみで、私用annexや秘密の鍵を読み出していない。

## 先行研究

- [FastCDC, USENIX ATC 2016](https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia): 内容に基づく区切りと重複排除。
- [DeepSketch, FAST 2022](https://www.usenix.org/conference/fast22/presentation/park): 学習による参照検索と可逆な差分圧縮。今回の語彙スケッチはこの論文の再実装ではない。
- [LLMZip, 2023](https://arxiv.org/abs/2306.04050), [Language Modeling Is Compression, 2023](https://arxiv.org/abs/2309.10668): 確率予測を可逆圧縮へ使う。
- [ts_zip](https://bellard.org/ts_zip/): 決定的なモデル評価・算術符号化の実装。通常圧縮より遅い点も比較する。
- [Learning Better Lossless Compression Using Lossy Compression, CVPR 2020](https://arxiv.org/abs/2003.10184): 近似表現と残差の可逆符号化。
- [Azure LRC, USENIX ATC 2012](https://www.microsoft.com/en-us/research/publication/erasure-coding-in-windows-azure-storage/): 冗長容量と修復readの両立。

初期成果は既存研究を組み合わせた再現可能な試験基盤。新規アルゴリズムの優位性、全量最適性、耐久性SLAの資格は今後の実測で判断する。


## 全量の暗号化永続custodyと隔離復旧試験

初回全量保存とWANコピーは完了し、両ノードの全断片/controlの独立読戻しも合格した。凍結epoch6の825,390 CID / 91,395,086,102 bytesが欠損0で対象になった。署名root SHAは`7a4fb7430b599ea594dc9a11415a0d1ae074554f71f3b7fe54269363f7f4d3c6`。Jacobのみを復旧元とするXavier側の全量ストリーム復元・EC修復と独立読戻しも完了した。Xavierのみを復旧元とするJacob側の全量元バイト保存・独立監査は進行中で、完了報告とは分ける。

| 完了済み実測 | bytes / 時間 |
|---|---:|
| 元lake全バイト | 91,395,086,102 |
| 暗号化圧縮パック | 20,969,055,218 |
| 1ノードの全6断片 | 31,466,056,986 |
| 1ノードの索引・鍵・control 3コピー | 762,739,302 |
| 2ノードの必要custodyファイル | 64,458,283,938 |
| 2ノードの必要custodyディスク割当 | 64,717,594,624 |
| 全量read/hash/encode/復元検証 | 104.24分 |
| 初回WANコピー・全CID復元・修復・同期 | 100.43分 |
| Xavier全断片/control独立読戻し | 6.78分 |
| Jacob全断片/control独立読戻し | 31.48分 |

初回WANコピーは21,045,433,967 wire bytes、復号15.32分、EC再生成・書込み35.16分、最終同期3.32分を含む。元lakeも全索引も受信側へ供給せず、索引再構築SHAを署名rootと照合した。受信側に元バイトの全量ファイルはまだ保存せず、全量ストリームを照合した。OSのキャッシュを排除しないcold process実測で、controller経由SSHの時間を含む。`results/full-persistent-build.json`、`results/full-initial-wan-restore.json`、`results/full-source-{xavier,jacob}-readback.json`が完了証拠。

`custody_build.cljk` は既存のCID付き元バイトを読むだけで、今回専用のAES-256-GCM鍵を使い、圧縮パックを暗号化して(6,4)断片を専用領域へ永続保存する。各ファイル・ディレクトリをfsyncし、48GiBの空き容量余裕を下回ると停止する。既存サービスの停止、WAN設定変更、pin解除、元データ削除、本番鍵の読み出しは行わない。

`custody_finalize.cljk` は全CIDの検証完了を要求し、inventory、パック台帳、CID→window→pack索引、鍵の指紋、復号ソフトのSHAを結び付けた研究用Ed25519署名rootを作る。索引・鍵・メタデータ・復号ソフトは各拠点に3コピー。署名rootの信頼SHAと公開鍵はパックとは別に固定する。今回の署名鍵は本番lake writer鍵ではない。暗号鍵コピーのファイル権限は600、controlディレクトリは700。鍵そのものをログ・Git・報告へ出さない。

`custody_campaign.cljk` は逐次実行し、各工程の状態とログSHAを残す。Xavierはnice 19 / ionice idle / 単一zstd worker、SSH転送は最大16MiB/s。初期保存の全量カバレッジと2拠点の永続読戻しが通らなければ、全量WAN復元へ進まない。失敗した専用領域も保持し、既存領域を上書きしない。

全量WAN試験は生存拠点だけから取得する。断片0/1、controlコピー0、全索引ファイルをexporterが読まない。生存拠点の鍵コピー2とメタデータコピー2を使い、受信側の新しいプロセスが復号・展開・全CID検証を実施する。パック内容から索引を再構築し、署名rootの索引SHAに一致させる。復元した暗号化パックから修復側の全6断片・全3メタデータ/鍵/索引コピーを新しい専用領域へ再生成する。

`custody_audit.cljk` は独立したプロセスで全6断片、全3control、全3パックmetadata/receiptをfsync・読戻し検証する。Jacobへの復元では元バイト全量を単一の復元アーカイブとCID/offset索引へ保存し、後の独立監査が全window SHAと全CIDを再検証する。Xavier側は空き容量を保つため元バイトをストリーム復元・照合し、修復ECを永続保存する。いずれも元lakeを復元時に読まない。

これは物理WAN停止・電源断ではなく、試験readerの入力制限で障害を注入する。共有ホストの性能に全く影響しないと断言するものではない。低優先度I/O/CPUと転送上限を使い、両ノードの既存Kubo version応答を10秒間隔で記録する。このプローブは全サービスのread/write保証ではない。ユーザー回答によりWAN・ルーター・電源の独立性は未確認または一部共有と扱う。常時scrub、自動修復、アクセス制御と鍵更新、信頼rootの長期custody、11-ninesの資格化も未完了。

初回Jacobコピーと読戻しの途中集計ではversion APIの失敗は0だったが、応答p95がbaseline約2.09msからコピー約29.53ms、監査約425.01msへ増加した。共有ホスト上の別負荷もあるため原因を断定しないが、無影響とは判定しない。`custody_io_guard.cljk` は専用研究rootのNode audit/restore/exportプロセスだけをPIDとargvで照合し、macOSの`taskpolicy -b`でI/O優先度を下げる。研究用以外のプロセスやサービス設定を変更しない。初回コピー・監査はnice 19、以後のJacob exporterからバックグラウンドI/Oが適用されたため、工程間の速度比較は同じ優先度条件ではない。適用時刻と対象PIDは専用rootの`io-policy-events.jsonl`に記録する。

実行手順は新規の専用パスを使用する。生成鍵を表示せず、既存の本番鍵を入力にしない。

```sh
TZ=UTC node custody_test.cljk /absolute/new/study-fixture
node custody_build.cljk --inventory /absolute/frozen.jsonl --sha256 INVENTORY_SHA --flatfs /absolute/blocks --raw /absolute/raw --out /absolute/new/custody --key /absolute/study-data-key.bin --zstd-workers 0
node custody_finalize.cljk /absolute/custody /absolute/frozen.jsonl /absolute/study-data-key.bin /absolute/study-signing.pem /absolute/new/trust.json
node custody_audit.cljk /absolute/custody /absolute/trust.json /absolute/new/audit.json
# exporter/receiverは異なる拠点のSSH接続で結び、stdin/stdoutのバイナリをログに出さない。
node custody_export.cljk /absolute/survivor/custody /absolute/trust.json degraded 16 | node custody_restore.cljk /absolute/trust.json /absolute/new/repair raw
node custody_campaign.cljk /absolute/private-sites-config.json /absolute/new/campaign-state
node custody_tco.cljk /absolute/completed-campaign/report.json /absolute/telemetry.jsonl /absolute/user-cost-inputs.json /absolute/new/tco.json
```

`custody_test.cljk` は複数パックをまたぐ18MiB超の元ブロックで、暗号認証、署名root信頼、主鍵/索引/metadataと2断片消失、索引再構築、全CID復元、修復読戻し、誤鍵拒否、ストリーム切断拒否を検証する。現行fixtureは31 checksで、同一ノードのhardlinkによる署名済みsource-view作成、元controlの不変性、全断片再監査も含む（復号コードのSHA変化と鍵ファイル権限の拒否、tape形式の2断片・主metadata・主鍵・全索引ファイルの実欠損からの再復元、復元アーカイブ用の補助offset索引の照合と改変拒否も含む）。費用算式は合成telemetryと実fixture容量で8 assertionsを検証した。これらのfixture試験と実lake全量の完了証拠を区別する。

Macのsandbox内で既定タイムゾーンを初期化する際にNodeがOS通知サービス待ちになる実行があった。停止した今回のfixtureだけを保全・終了し、新しいfixtureへ `TZ=UTC` を指定して24 checksを通した。署名rootの日時はUTCのISO表記を使い、OS設定は変更しない。未実行の監査には補助offset索引の全行照合を追加し、初回Xavier監査で使った旧コードも保全した。新しい監査報告は開始時に取得した監査コードSHAを含む。稼働中のbuilder・exporter・receiverと署名済み復号assetは変更しない。

`custody_telemetry.cljk` は[NVIDIA公式のAGX Xavierレール定義](https://docs.nvidia.com/jetson/archives/r35.6.4/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonXavierNxSeriesAndJetsonAgxXavierSeries.html)に沿ってGPU/CPU/SOC/CV/VDDRQ/SYS5Vを読む。AC電源、電源変換損失、外部ディスクの測定値ではなく、既存処理も含む。Jacobのpowermetricsはパスワードなしの権限がなく、管理者設定は変更しない。

TCOはユーザー提供の購入額（2台合計150,000円）と電気単価27円/kWhを使う。未提供の償却期間・保守時間/単価・AC電力・外部ディスク購入額を測定済みとしない。3/5年の定額償却、2台合計20/40/60W、保守0/0.5/2時間/月×仮定3,000円/時の18ケースで小計を出す。保守0は楽観的な下限であり、実運用工数ゼロの証拠ではない。全ホスト負担と共有容量按分を分け、未知の費用を残した実TCOはnullとする。米ドルとの換算は100/150/200円の感度だけで、実為替レートではない。

| ユーザー提供の実額 | 金額・時期 |
|---|---|
| Xavier、中古 | 50,000円、2026年8月購入 |
| Jacob、新品 | 100,000円、2025年8月購入 |
| 電気料金 | 27円/kWh |

全ホスト負担の償却小計は残存価額0の仮定で月2,500円（5年）または4,166.67円（3年）。合計20/40/60Wの仮定では電力小計が月394.20/788.40/1,182.60円となる（730時間/月）。保守の仮定は月0/1,500/6,000円。これらを合計した月2,894.20–11,349.27円は感度分析の小計で、外部ディスク、回線、交換予備、保証維持などの未確定費用を含む実TCOではない。購入日は入力の出所として保存し、この試算を税務上の償却判定には使わない。

クラウドの容量対照にも暗号化圧縮本体と1論理セットの索引・inventory・root・復号ソフト・鍵を含める。自前の必要custody容量と、研究の監査台帳・修復用コピー・復元アーカイブを含む実占有容量を分けて集計する。

割当量は通常ファイルの `stat.blocks` で、ディレクトリ・inode・journal・空き容量余裕の管理領域を含まない。TCOの資格済み保存領域の集計範囲は全量campaignの4つのcustody/repair領域である。全研究領域の占有量は `custody_study_allocation.cljk` で別途集計し、保全した未完了試験、source-view、runtimeとログも含める。同一dev/inodeのhardlinkは一度だけ数え、鍵の内容は読まない。元lake、ローカルLLMモデル・controllerファイルはこの遠隔研究領域の外に保持している。今回のSSH経路はcontrollerを中継するため、そのホストの電力・設備按分、研究開発の作業費・モデル/API費も未確定費用として残す。


### HDD向け連続断片形式

実リンクの18.94MB fixtureでは、Jacobの外部HDDでパックごとにファイル/ディレクトリを同期する形式が修復約103秒・全工程約182秒になった。全量へこの時間を外挿して採用しない。`custody_tapes.cljk` は6断片位置それぞれを1つの連続ファイルへまとめ、manifest/receiptも3つの連続ファイルへ保存する。全パックを書き終えてから全ファイルとディレクトリをfsyncし、完了マーカーを作る。未完了のtapeは復旧元として拒否する。個々のパックの署名catalog→manifest SHA→断片SHAの信頼関係は維持する。

同じサイズの別fixtureで連続形式は修復書込み約0.87秒、最終同期・control保存約64.85秒、全工程約70.72秒になった。多数のパックごとの同期を減らし、最終同期を含む完了後の永続性を検証する。これは小さいfixtureの実測で、全lakeの時間は別途測る。初期のJacobコピーも4断片の実WAN復元から直接連続形式へ保存し、全CID/索引検証を通してから2拠点custodyを確定する。

既存Kubo応答はXavierのAPI 5001に加え、Jacobの既存custody設定が使用するAPI 5002で記録する。最初に調べた5001はJacobの実ポートではなかった。サービスの設定・再起動は行わない。復旧ソフトのJavaScript sourceを保存・SHA検証するが、OSとインストール済みNode runtimeのbootstrapは別の前提として残る。

全量転送中に受動監視の6時間上限へ届く可能性が出たため、`custody_observer_restart.cljk` で今回専用のobserverを1つずつ識別し、監視だけを継続した。`--dry-run` で対象PIDを確認し、元コードを保全してから、そのPIDだけを終了し同じログへ明示的な `--append` で追記する。全量receiver、既存サービス、WAN設定は変更しない。旧・新コードと継続前ログのSHA、継続時刻を `observer-restarts.jsonl` に記録する。既存サンプルは保持し、監視の継続境界は10秒間隔の測定として扱う。

Jacobへの初回全量コピーで、研究用receiverだけをnice 19で1秒・10ms間隔サンプリングした。主スレッド63サンプルのうち62がファイル書込みの `write` 呼出し内だった。receiverのCPU使用率が低かった観測と合わせ、この時点では受信側の書込み待ちが速度を制限した可能性がある。共有HDDの他処理や短い観測範囲を含むため、全工程の時間やディスク単独性能へは外挿しない。`results/receiver-write-profile.json` に範囲と元記録SHAを保存する。小さいfixtureの連続断片形式の結果だけで、全lakeのHDD速度を予測しない。

新しいreceiverは索引2種を256KiB、元バイトを4MiBの単位へまとめて書く。短い書込みの再試行を処理し、全バッファをflushしてからfsyncと完了報告を行う。4,098 CID（4,096個の128-byteブロックとテキスト、18MiB超ブロック）のローカルfixtureで全量バイト一致、署名索引一致、独立offset監査、改変と切断の拒否を検証した。`results/buffered-write-fixture.json` に論理書込み数と実write呼出し数を記録する。これはローカルfixtureの測定で、全lake・WAN・Jacob HDDの速度向上を示さない。ローカルfixtureのnice指定はsandboxで拒否されたため通常優先度で実行した。遠隔研究プロセスの優先度設定とは分ける。

旧campaignのJacob元バイト保存試験は1,000パック / 16,135,666,517 bytesまでで退役し、未完了ファイルを保全した。専用receiver・exporterとそのSSHクライアントだけを所有者とargvで特定して終了した。既存サービス、元lake、元custody、WAN設定を変更しない。旧campaignは失敗状態のまま保持し、全量完了とは呼ばない。

`custody_source_view.cljk` は全量監査済みの暗号化パックを同じノードでhardlinkし、新しいreceiverだけを署名assetとして更新したcontrolを別領域へ作る。元controlは保持する。これを追加の物理レプリカとは数えない。新しいsource-viewの全断片/control読戻しは合格した。旧rootは `7a4fb7430b599ea594dc9a11415a0d1ae074554f71f3b7fe54269363f7f4d3c6`、新rootは `b94da2fe580193e16fd1e2732d522c2dc7631ffc7df82b106e22721dcf303a64`。署名公開鍵、inventory、catalog、索引、暗号化形式、データ鍵SHAと他の復号assetは一致する。両rootの互換性を全量監査とともに記録し、進行中のプロセスのコードは差し替えない。

`custody_buffered_campaign.cljk` は資格済みの初回全量コピー・両元custody監査・Xavier修復を引き継ぎ、新rootでJacobへの全量元バイト保存と全CID/offset独立読戻しを行う。新旧受信側の全量バイトSHA一致も完了条件にする。Jacobはnice 19とbackground I/Oを維持する。最初の100パックは約402秒で、旧試験の約261秒より遅かった。書込み呼出し数削減から実リンク速度向上を推定せず、全量完了時間と監査時間を別途測定する。
