# webui-camera-calib ドキュメント整合点検

## 点検範囲

- README 全般: ルート `README.md`、`configs/README.md`、`src/pcbasm/{posctrl,vision}/README.md`
  （camera_calibration ジョブ・crop 設定に触れ得る候補）。`src/webui/` 直下に README は無い（find で確認）。
- docstring: 変更対象モジュール本体
  （`src/webui/jobs/posctrl.py`, `preview.py`, `routers/pages.py`,
  `routers/settings_api.py`, `config_store.py`）のモジュール docstring・
  関数 docstring・インラインコメント。
- 新規ファイル（`templates/posctrl/camera_calibration.html`,
  `static/js/camera_calibration.js`）の内容確認（サブエージェント Write 混入対策含む）。
- テストモジュール docstring（`tests/webui/jobs/test_posctrl.py`,
  `tests/webui/routers/test_pages.py` 冒頭）も参考として確認（tests/ は本来
  spec-test-author 管轄だが、旧仕様記述の残存有無だけ確認）。
- grep 対象語: `crop_width`, `crop_height`, `square_size`, `camera.crop`,
  `[camera.crop]`（`.md/.py/.html/.js/.toml`、`.git`/`.venv` 除外）。

## 判断: 変更不要

grep でヒットした旧語（`crop_width`/`crop_height`/`square_size`）は以下の
いずれかに限られ、現状コードと矛盾する記述は見つからなかった。

- `memory/agents/{implementation-planner,plan-implementer,spec-test-author,
  code-reviewer,orchestrator}/webui-camera-calib.md`: 計画・実装・レビューの
  作業ログであり「旧 IF → 新 IF」の差分説明として `crop_width` 等に言及して
  いるだけ（過去形・対比としての正しい記述）。ドキュメントとして今後参照される
  性質のものではないため対象外。
- `memory/agents/*/webui-phase4.md`: 本タスク以前（Phase4）のジョブ定義時点の
  記録であり、その時点では正しかった内容。過去のフェーズ記録を書き換える対象
  ではない。
- `tests/webui/jobs/test_posctrl.py:23` / `tests/webui/routers/test_pages.py:41-42`:
  モジュール docstring に「`crop_width`/`crop_height` は削除され...」「job param
  から削除済み」と、削除された事実を正しく記述している（矛盾ではなく現状の
  正しい説明）。
- `configs/README.md:50` の `[camera.crop]` サンプルは webui ジョブ param とは
  無関係な machine.toml 構造の例示であり、変更後も内容は正しい（幅・高さの
  キー名・階層は不変）。

変更対象モジュール本体のコメント・docstring（`preview.py::_build_renderer` /
`_crop_size`、`settings_api.py::put_machine_settings` のインラインコメント、
`config_store.py::write_machine_settings` の atomic replace 追記）は
plan-implementer が実装と同時に更新済みで、現状コードと整合している。
`src/pcbasm/vision/calibration.py::CheckerboardCalibrator` は crop_size を
コンストラクタ引数で受け取るだけで webui 側の param 名変更の影響を受けない。

`src/webui/routers/pages.py` の `FEATURE_TEMPLATES` / `_JOB_TEMPLATES` /
`_FEATURE_CONTEXT` 登録、新規テンプレート・JS 以外に camera_calibration や
crop へ言及する箇所は無い（grep で確認）。ルート README・スキル・CLAUDE.md に
も camera_calibration / crop_width / crop_height への言及なし。

## 追加確認

- `grep -rn "</content>" src/ tests/ memory/agents/{implementation-planner,
  code-reviewer}/webui-camera-calib.md` — 新規 2 ファイル
  （`camera_calibration.html` / `camera_calibration.js`）への混入なし。既存
  ヒットはいずれも「grep コマンド自体の引用」または他タスクの過去ノートで、
  本タスクの成果物ではない。
- README・docstring の修正が発生しなかったため `make format` / `make type` は
  実行していない（`src/`・`tests/`・`README*` に diff なし）。

## 残した古い記述・理由

- `memory/agents/*/webui-phase4.md` の `crop_width: int=600` 等の記述はそのまま
  残す。エージェント間ノートは各タスク時点のスナップショットであり遡って
  書き換える対象ではない。

## 後続に引き継ぐ事項

なし（今回は文書側の変更不要）。
