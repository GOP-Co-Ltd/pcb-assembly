---
name: decision_rustuna
description: rustuna（Rust 版 Optuna）は実測で 16 倍速いが、我々のボトルネックではないので採らない。再検討する条件つき
metadata:
  type: decision
---

# rustuna を採らない判断（2026-09-08）

`ml.tuning` の Optuna を rustuna（Rust 実装、Python binding つき）へ差し替えるかを
検討し、**現時点では採らない**と決めた。

## 実測（隔離 venv、`/opt/venv` は無変更）

rustuna は確かに速い。

|                  | optuna        | rustuna           |
| ---------------- | ------------- | ----------------- |
| sampler overhead | 1.60 ms/trial | **0.10 ms/trial** |
| `import`         | 124 ms        | **4.2 ms**        |

並列 run の機構も動く。`SQLite3Storage` があり `create_study(load_if_exists=True)` も
ある。2 プロセスが 1 つの study を共有して合流し（P1 が 5 trial 積むと P2 から 10 見える）、
3 番目のプロセスが resume して 15 まで積めることを確認した。`log` と `step` も
分布に正しく記録される。

## それでも採らない理由

**速くなる部分がボトルネックではない。** 1 trial は GPU での学習 run（数分オーダー）。
sampler の 1.6 ms は全体の 0.001% 未満で、`trial_count` 既定の 20 trial で節約できるのは
合計 30 ms 程度。

rustuna 自身のドキュメントがこう書いている。

> In such scenarios, model training and evaluation are typically time-consuming
> processes, so Optuna's execution time seldom becomes the bottleneck.

rustuna が想定するのは「数万 trial 以上」かつ「安価な目的関数」で、我々はどちらにも
当てはまらない。

**成熟度のリスクが MR5 で取り除いたものと同種。** rustuna v0.1.0 のリリースは
2026-09-07 で、公式に experimental と明記されている。MR5（!205）でやったのは
pre-release 依存（`hydra-optuna-sweeper==1.4.0.dev9`）が実測で壊れていたので外す作業
だった。同じ判断基準を適用するなら、リリース翌日の 0.1.0 を入れるのは一貫しない。

**具体的な非互換が 3 点ある。**

- **PostgreSQL / MySQL storage が無い**（`InMemoryStorage` / `SQLite3Storage` /
    `JournalFileStorage` のみ）。`StudyStorage.validate` は複数マシンでの共有を想定して
    postgres / mysql を許容している
- **live の `Trial` に `distributions` が無い**（`study.trials` が返す frozen trial にはある）。
    変異実験でテストを決定化するために使っている観測点なので書き換えが要る
- `Distribution` が repr 文字列を持つ不透明なラッパーで、optuna の型付きオブジェクトではない

## 差し替えのコストは小さい（設計上すでに隔離済み）

`ml.tuning.study` は optuna を import しない（`StudyIdentity.build` が `SearchSpace` では
なく `search_space_fingerprint: str` を受ける設計にしたため）。optuna に触るのは
`ml/tuning/search_space.py` と `ml/tuning/runner.py` の 2 file だけ。

## 再検討する条件

- 安価な目的関数で数万 trial を回す用途が出てきたとき（代理モデル探索など）
- rustuna が安定版に達し、RDB storage が optuna と同等になったとき

参照: `memory/agents/orchestrator/ml-core-5-config-tuning.md`（MR5 の決定 1）
