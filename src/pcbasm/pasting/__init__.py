"""ペースト塗布の制御.

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する
（例: ``from pcbasm.pasting.applicator import PasteApplicator``）。
``import pcbasm.pasting`` 自体は cv2 / pcbnew などの重い依存を読み込まない。

通常塗布ジョブ（``web.api.jobs.pasting.paste_solder``）の流れと担当モジュール:

1. 装置を動かす前に、対象 pad・順路・初回パージ・運転時補正を決める: :mod:`.plan`（設定解決は :mod:`.overrides`、順路は :mod:`.route`。pad 編集画面の preview も同じ計画を使う）
2. Board 計測の結果から :class:`~.session.PasteSession` を組み、:func:`~.prepare.measure_correction` が高さ面計測と位置合わせで :class:`~.prepare.PasteCorrection` を作る
3. ``with session.make_applicator()`` でディスペンサーを有効化し、``retract()`` する
4. 初回パージを点塗布する（:mod:`.purge_check`。写らなければ :mod:`.nozzle_clean` で掃除してやり直す）
5. 運転時補正（:mod:`.paste_volume.runtime_correction`。撮影は :mod:`.dot_capture`）の結果で ``rotations_per_ul`` を差し替える
6. pad ごとに :meth:`~.applicator.PasteApplicator.apply` で塗る（経路は :mod:`.fill_path`、G-code は :mod:`.fill_sequence`）

運転者への問い合わせ（prompt）と成果物は web ジョブが持ち、このパッケージは持たない。
手順のログ・進捗・中断は :class:`~.hooks.ProcedureHooks` を通してジョブへ渡す。

2 つの「流量キャリブレーション」を混同しない:

- :mod:`.dispense_calibration`: 銅板に線を引いて計量し、``machine.toml`` の ``rotations_per_ul`` などを決める事前校正
- :mod:`.paste_volume.runtime_correction`: 塗布ジョブ中に基板上のドットを撮影して ``rotations_per_ul`` をその場で補正する（``machine.toml`` は書き換えない）

その他: :mod:`.paste_volume` は点の直径 → 体積の校正（その校正用の画像収集は :mod:`.paste_volume.dataset`）、:mod:`.test_board` はテスト塗布基板の生成。

単位: 長さ・座標は mm、量は μL、``rotations_per_ul`` は rev/μL。
"""
