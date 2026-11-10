# SPDX-License-Identifier: GPL-3.0-only
#
# pnp-motion ファームのホスト側の作業。.uf2 のビルドは build.sh で行う。
#   make test           core をホストの cc でテストする（UBSan つき）
#   make test OUT=dir/  ビルドの出力先を変える（既定はリポジトリの外のキャッシュ）

CACHE_DIR ?= $(or $(XDG_CACHE_HOME),$(HOME)/.cache)/pcbasm/pnp-motion
OUT ?= $(CACHE_DIR)/test/
CC ?= cc
CFLAGS := -std=gnu11 -O1 -g -Wall -Wextra -Werror \
    -fsanitize=undefined -fno-sanitize-recover=all -Icore

.PHONY: test
test: $(OUT)test_vs_planner
	$(OUT)test_vs_planner

$(OUT)test_vs_planner: tests/test_vs_planner.c core/vs_planner.c core/vs_planner.h
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -o $@ tests/test_vs_planner.c core/vs_planner.c
