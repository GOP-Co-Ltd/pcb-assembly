"""Backend を mDNS (DNS-SD) で広告し探索するときのワイヤ定数と、API 契約の版."""

from __future__ import annotations

# backend が `GET /api/machine-info` で自己申告し、mDNS の広告にも載せる API 契約のバージョン。
# frontend の探索は版のずれた機体を更新専用にする。
# frontend は machine-info の api_version を見ないので、静的登録だけの機体は更新専用にならない
API_VERSION = 11

# DNS-SD のサービス型
SERVICE_TYPE = "_pcbasm._tcp.local."

# TXT レコードのキー（値は UTF-8 bytes で載る）
TXT_KEY_ID = "id"
TXT_KEY_API = "api"
