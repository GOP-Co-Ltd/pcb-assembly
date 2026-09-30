"""ペースト塗布の制御.

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import する
（例: ``from pcbasm.pasting.applicator import PasteApplicator``）。
``import pcbasm.pasting`` 自体は cv2 / pcbnew などの重い依存を読み込まない。

通常塗布ジョブ（``web.api.jobs.pasting.paste_solder``）の流れと担当モジュール:

1. 装置を動かす前に、対象 pad・順路・初回パージ・運転時流量キャリブを決める: :mod:`.workflow`（設定解決は :mod:`.settings`、順路は :mod:`.route`）
2. Board 計測の結果から :class:`~.session.PasteSession` を組み、高さ面計測と位置合わせで :class:`~.alignment.PasteCorrection` を作る
3. ``with session.make_applicator()`` でディスペンサーを有効化し、``retract()`` する
4. 初回パージを点塗布する（写らなければ :mod:`.nozzle_clean` で掃除してやり直す）
5. 運転時流量キャリブ（:mod:`.paste_volume.runtime`）の結果で ``rotations_per_ul`` を差し替える
6. pad ごとに :meth:`~.applicator.PasteApplicator.apply` で塗る（経路は :mod:`.fill_path`、G-code は :mod:`.fill_sequence`）

判断・ログ・進捗・中断は web ジョブが持ち、このパッケージは持たない。

2 つの「流量キャリブレーション」を混同しない:

- :mod:`.flowcalib`: 銅板に線を引いて計量し、``machine.toml`` の ``rotations_per_ul`` などを決める事前校正
- :mod:`.paste_volume.runtime`: 塗布ジョブ中に基板上のドットを撮影して ``rotations_per_ul`` をその場で補正する（``machine.toml`` は書き換えない）

その他: :mod:`.paste_volume` は点の直径 → 体積の校正、:mod:`.dataset` はその校正用の画像収集、:mod:`.testboard` はテスト塗布基板の生成。

単位: 長さ・座標は mm、量は μL、``rotations_per_ul`` は rev/μL。
"""
