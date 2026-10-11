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
| LLM entropy coding | 確率予測 + 算術符号化、版固定モデルの保管 | 先行研究調査のみ。実モデル測定は未実施 |

`.cljk` で Node 組込み機能だけを使用。研究用 `nbb.edn` はサービスの依存解決から独立している。
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
ファイル書込みは実験用で fsync / 電源断 / WAL / WAN 分散 / 修復デーモンを実装していない。
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
- [Filecoin](https://www.filecoin.io/store-data): サービス・プロバイダ・契約と取得条件を揃えた現行見積もりが必要。
  [Onchain Cloud発表](https://www.filecoin.io/blog/introducing-filecoin-onchain-cloud)の価格はearly-adopter期間限定で、今回の現行価格として扱わない。

この91.4 GBを未圧縮で保管した容量課金だけの概算はR2約$1.37/月、B2約$0.64/月（無料枠等を除く）。
小さな絶対額なので、自前ノードを増やす費用やLLMのGPU計算費用が節約額を超える可能性がある。
`storage_cost.cljk` は仮定を明示して損益分岐点を計算する。

`C_yata = logical_GB × measured_physical_ratio × node_cost_per_GB_month + other_costs`

`other_costs` にハード償却、電力、モデル・索引、符号化、取得・修復帯域、運用、保証水準の費用を入れる。
未知の自前単価をゼロとみなさず、Filecoinの料金と耐久性・取得性能も固定値で推測しない。
現時点で3サービスより総費用・性能が優れているとは証明していない。

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
