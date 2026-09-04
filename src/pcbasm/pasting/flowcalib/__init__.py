"""流量キャリブレーション（rotations_per_ul / max_dispense_rate / max_fill_speed）.

re-export は持たない。サブモジュールを直接 import する。

- :mod:`~pcbasm.pasting.flowcalib.params`: ジョブパラメータ（既定値の唯一の出典）
- :mod:`~pcbasm.pasting.flowcalib.flow`: 質量計測 → 係数の数理と掃引の量計算
- :mod:`~pcbasm.pasting.flowcalib.lines`: 銅板上の線配置と掃引点の計画
- :mod:`~pcbasm.pasting.flowcalib.procedure`: 銅板・transform・applicator を束ねる HAL 側手順
- :mod:`~pcbasm.pasting.flowcalib.board`: 流量キャリブレーション基板（KiCad）の生成
"""
