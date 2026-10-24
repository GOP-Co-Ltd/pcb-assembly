"""吐出の事前校正（rotations_per_ul / max_dispense_rate / max_fill_speed）.

銅板に線を引いて計量し、``machine.toml`` の値を決める。塗布ジョブ中の補正は :mod:`pcbasm.pasting.paste_volume.runtime_correction`。

re-export は持たない。サブモジュールを直接 import する。

- :mod:`~pcbasm.pasting.dispense_calibration.params`: ジョブパラメータ（既定値の唯一の出典）
- :mod:`~pcbasm.pasting.dispense_calibration.mass`: 質量計測 → 係数の数理と掃引の量計算
- :mod:`~pcbasm.pasting.dispense_calibration.lines`: 銅板上の線配置と掃引点の計画
- :mod:`~pcbasm.pasting.dispense_calibration.procedure`: 銅板・transform・applicator を束ねる HAL 側手順

テスト塗布基板（KiCad）の生成は別パッケージ :mod:`pcbasm.pasting.test_board` にある。
"""
