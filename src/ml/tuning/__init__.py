"""ハイパーパラメータ探索の同一性・探索空間・runner.

並列 run は「1 個の RDB study を複数の OS プロセスが共有する」ことで達成する。

プロセスを起こすのは運用者の仕事なので、ここに supervisor は置かない。2 GPU の
機体なら次のように 1 プロセスずつ起こす。

.. code-block:: shell

    CUDA_VISIBLE_DEVICES=0 <train> --study X --storage sqlite:////data/X.db &
    CUDA_VISIBLE_DEVICES=1 <train> --study X --storage sqlite:////data/X.db &

同じ ``study_name`` と storage を指すプロセスは、``load_if_exists`` で同じ study へ
合流し、trial を積み増す。

このパッケージは re-export を持たない。利用側は必要なサブモジュールを直接 import
する（例: ``from ml.tuning.study import StudyIdentity``）。
"""
