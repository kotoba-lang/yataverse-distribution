# yataverse lake 復元保証付き保存の研究開発 — 2026-10-11

既存 lake 全体を対象に、保存量・復元・計算費用・障害ドメインを評価する。
このディレクトリは実行可能な研究試作であり、本番の writer / reader / custody / R2 / B2 / Filecoin 設定は変更しない。
現段階では「LLM / Vector DB が既存方式を上回った」という結論は得られていない。
今回の実 lake サンプルでは **solid zstd + 消失訂正**が最良だった。

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
電源断試験、WAL、常時修復デーモン、全lakeのWAN復元はまだ実施していない。
したがって、数学的な断片復元とローカル試験は、運用時の耐久性・SLAの保証とは区別する。

`validate-placement` は拠点ごとの断片数も検査する。
6拠点に1断片ずつなら任意の2拠点消失、3拠点に2断片ずつなら任意の1拠点消失に対応する。
2 WAN に3断片ずつ置く構成は、1 WAN 消失で3断片失うため拒否する。
2拠点しか使えずどちらかの拠点消失から完全復元したい場合、各拠点に全情報を持たせる必要があり、
非圧縮情報に対する物理容量の下限は2倍になる（その後の保存中の変化・追加故障は別）。

## lake の全インベントリと実バイト測定

2026-10-07 epoch 6 のローカル凍結インベントリ `log-resolved.jsonl` を評価時に読み直した。
SHA-256: `2334fb3efefc460a9ed1301aecfa2b5183133fc0bb0208a93692617672eb8a91`。
825,390 unique CID / 91,395,086,102 bytes（約91.4 GB）。これは全行のメタデータ走査で、全ブロックの読み出しではない。
最新 inga head と署名の再検証は今回の研究CLIでは行っていない。

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
2拠点のWAN独立性は既存構成の申告を前提とする。外部IPのtrace確認は自動承認審査で拒否されたため実施しなかった。
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
この58.3GB部分の比率を91.4GB全体へ外挿しない。欠損集合をXavierで補完してから全行台帳を突き合わせる。
この長いローカル実行の途中で作業ソースを編集したため、終了時の作業ファイルSHAと実行コードSHAが異なった。
元の未加工報告を保持し、開始前にXavierへコピー済みの同一ソースのSHA
`d0db498d84743b6dc59a3ce63708955cb6d0ae5fca827390933e8545c2f73d99`をruntime provenanceとして明示した。
次の実行ではソースSHAを開始時に固定し、別名の実行用コピーを編集しない。

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
CLIは最大256 MBの1ブロックを読み込む。大量実行にはストリーム処理と永続チェックポイントの実装が次に必要。
flatfsのlayoutはmultihash/base32/next-to-last-2限定。未知のlayout・hash・サイズは拒否する。

## 全量の次段階

1. 最新の署名済み lake head に結び付いた全インベントリを凍結し、未読CIDと大ブロックも含めて全バイトを評価する。
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
