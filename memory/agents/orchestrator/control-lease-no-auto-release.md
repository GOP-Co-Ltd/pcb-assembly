# 操作権の自動解放をやめる

## 段階 1 計画

要求: 操作権の自動解放（WS 切断 30 秒・無操作 10 分）を廃止する。解放されるのは
明示の解放・奪取と、プロセス再起動（リースはメモリ上なので消える）だけ。

公開 IF の変更:
- `ControlLease(*, on_change=None)`。clock / busy / disconnect_grace / idle_timeout を削除
- `create_app` の `clock` 引数を削除（リースの時計専用だったため）
- `connect` / `disconnect` は接続数（LeaseInfo.connections の表示用）を数えるだけになる

保つもの: 奪取（誰も操作できなくなる状態からの脱出口）、claim の排他、on_change の通知

テスト観点:
- 保持者の WS が全部切れても / 一度も繋がなくても保持のまま
- 接続数は数え続ける
- 引数なしで構築できる（時計に依存しない）
- API: WS 切断後も held のまま（旧 TestIdleExpiry を置き換え）

却下: 長めの猶予に変える案（要求は「基本的に無し」）
関連: PR #45（一括管理）の bulk.js コメントは 30 秒失効を前提に WS を張る理由を書いている。
本 PR と #45 の後にマージされる側で追従が要る

## 段階 2〜4

- test_control.py: 時間経過系（TestWebsocketPresence / TestIdleTimeout / 失効の同時奪取）を削除し、
  TestNoAutoRelease に置換。API 側は TestIdleExpiryIsGatedByTheMachineLock を TestNoAutoRelease
  （切断後も 423、再起動で空き）に置換
- 限界: 「時間が経っても解放しない」は時計を持たない構造で担保する（time のモックは規約で禁止）
- 自己レビュー: docformatter の行結合で入った空白を直した。他に指摘なし
