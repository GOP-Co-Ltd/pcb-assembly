# air_pump_enabled オプションの削除

エアポンプを無効化することは実運用で無いため、`air_pump_enabled` 設定を pcbasm / webui から削除し、
エアポンプは常に有効（`printer.cfg` の `[output_pin air_pump]` は必須）とする。

## 段階 1: 計画

### ユーザー確認

「air pump の on/off オプション」の解釈を確認 → **`air_pump_enabled` 設定の削除**（採用）。
`AirPump.on()/off()` とディスペンサー enable/disable 時のポンプ ON/OFF 制御は維持する。

### 公開インターフェースの変更

- `PasteDispenserConfig.air_pump_enabled: bool` — 削除
- `PasteDispenser.__init__(klipper, rotations_per_ul, stepper_name)` — `air_pump_enabled` 引数を削除。
  `AirPump` は常に生成し、`[output_pin air_pump]` 欠落時は常に `RuntimeError`
- `PasteDispenser.enable()/disable()` — 常に AirPump ON/OFF + Stepper Enable/Disable
- WebUI machine settings のホワイトリストから `paste_dispenser.air_pump_enabled` を削除

### 変更ファイル

| ファイル | 内容 |
| --- | --- |
| `src/pcbasm/config.py` | フィールド削除 |
| `src/pcbasm/hal/paste_dispenser.py` | 引数・分岐・docstring 削除 |
| `src/pcbasm/session.py` | 受け渡し 2 箇所削除 |
| `src/webui/config_store.py` | FieldSpec 削除、`bool` 型サポート削除 |
| `src/webui/jobs/pasting.py` | 受け渡し 2 箇所削除 |
| `src/webui/templates/settings.html` | bool 分岐削除 |
| `src/webui/static/js/settings.js` | bool 分岐削除 |
| `data/config-templates/kurousagi.paste/machine.toml` | キー削除 |
| tests | 該当テストの削除・更新 |

### 計画外の判断

- **machine settings の `bool` 型サポートも削除する。** `air_pump_enabled` が唯一の bool
  フィールドで、削除後は `_coerce` の `case "bool"`・`settings.html` の checkbox 分岐・
  `settings.js` の `dataset.type === "bool"` が到達不能になるため（CLAUDE.md 開発原則 3:
  自分の変更で生じた orphan は消す）。ジョブパラメータ側の bool（`jobs/catalog.py` の
  `ParamSpec`）は別系統なので触らない。
- **既存 `machine.toml` に残る `air_pump_enabled = true` 行は無視される。** cattrs の既定は
  `forbid_extra_keys=False` のため読み込みエラーにならない。移行処理は入れない。

## 段階 2: テスト実装

削除のみの変更のため red フェーズは無い（skill の「純リファクタは段階 2 省略可」に準ずる）。
削除したテスト:

- `tests/pcbasm/hal/test_paste_dispenser.py`: `mock_klipper_no_air_pump` フィクスチャ、
  `test_init_air_pump_disabled_skips_section_check`、`test_air_pump_disabled_omits_set_pin`
- `tests/pcbasm/test_config.py`: `test_air_pump_enabled_defaults_true_when_absent`、
  `test_air_pump_enabled_explicit_false`
- `tests/webui/test_config_store.py`: `TestAirPumpEnabled`
- `tests/e2e/test_webui_e2e.py`: `TestAirPumpToggleOverRealHttp`

`test_numeric_field_still_rejects_bool` だけは仕様として残るため `TestMachineSettings` へ移し
`test_numeric_field_rejects_bool` に改名した。

## 段階 3: 機能実装

計画どおり。`make format && make type && make test-no-hardware` green（1674 passed）。
WebUI の設定画面テンプレートに触れたため `make test-e2e` も実行（52 passed）。

## 段階 4: 自己レビュー

`git diff` を通しで読み、以下 1 点を追加対応した。

- **`MachineSettingValue` から `bool` は外さない。** 外すと pydantic の lax 変換で
  JSON の `true` が数値フィールドの `1` として素通りしうる。`_coerce` 冒頭の
  「bool は受け付けません」で 400 にするために union に残し、理由をコメント化した。
  この経路を実 HTTP で固定する `test_put_bool_value_returns_400` を追加。

その他、diff の全行が要求（および上記の orphan 削除）からトレース可能であることを確認した。

## 段階 5: ドキュメント

docstring は段階 3 で同期済み。README / skill / CLAUDE.md に `air_pump_enabled` の記述は無く、
同期対象なし。`printer.cfg` テンプレートの `[output_pin air_pump]` は必須のまま維持。

## ユーザー確認事項

- 実機での塗布動作確認（エアポンプ ON/OFF が従来どおりか）は未実施。`@mark_hardware` は
  Claude が実行しないため、ユーザー側で確認が必要。
- 稼働中の `machine.toml` に残る `air_pump_enabled = true` 行は無視されるだけなので削除は任意。
