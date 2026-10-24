"""ジョブ（長時間の装置操作を、ログ・進捗・問い合わせ・中止付きで画面から走らせる単位）.

ジョブは 1 ファイル 1 ジョブで、ファイル名がジョブ名（画面の feature slug）になる。
ディレクトリはジョブが呼ぶ pcbasm のドメインをミラーする。
ジョブの一覧は :mod:`web.api.jobs.catalog`、実行基盤は :mod:`web.api.jobs.runtime`、複数のジョブが使う台本の部品は :mod:`web.api.jobs.steps` にある。
"""
