"use strict";

// 更新ページ（/update）のパネル。UI サーバー自身と機体ごとに 1 枚ずつあり、独立して動く。
// エンドポイントとトランスポートはパネルの data-* で切り替える。
//
// このファイルはロジックを持たない。「同居機なので画面も切れます」等の文言、状態の
// 要約、手順のラベルは、すべてサーバが status API で返した文字列をそのまま表示する。
(() => {
  const { api, frontendApi, toast, createBackoff } = window.webui;

  // 再起動後の復帰を待つ上限。超えたら待機をやめて、確認方法を表示する
  const RECOVERY_LIMIT_MS = 180000;

  function setupPanel(panel) {
    const { statusUrl, checkUrl, runUrl, transport } = panel.dataset;
    // 機体の URL（/m/<machine_id>/api/update/**）は proxy が backend へ中継する。api() は
    // このページの空の prefix しか付けないので、そのまま機体宛てになる（423 は control.onDenied へ）。
    // frontend 自身のエンドポイントは素の fetch で呼ぶ。
    const call = (method, url, body) =>
      transport === "frontend"
        ? frontendApi(method, url, body)
        : api(method, url, body);

    const el = (name) => panel.querySelector(`[data-update="${name}"]`);
    // パネルが並ぶので、どのホストの知らせかを見出し（ホスト名）で示す
    const notify = (message, level) => toast(`${el("host").textContent}: ${message}`, level);
    const setText = (id, value) => {
      el(id).textContent = value || "---";
    };
    const setNote = (id, value) => {
      const node = el(id);
      node.textContent = value ?? "";
      node.hidden = !value;
    };

    let latest = null;
    let watching = null;

    function renderSteps(run) {
      const list = el("steps");
      list.replaceChildren();
      for (const step of run.steps ?? []) {
        const item = document.createElement("li");
        item.className = step.ok ? "ok" : "ng";
        item.textContent = step.label;
        list.appendChild(item);
      }
      // 失敗した手順の出力はサーバが選んで返す（steps から JS で再導出しない）
      setNote("detail", run.failed_detail ?? "");
    }

    function renderWarnings(run) {
      const list = el("warnings");
      // 版がずれた backend が返さないフィールドで TypeError を投げない
      // （pollOnce の try の外なので、投げるとポーリングがエラー表示なしで止まる）
      const warnings = run.warnings ?? [];
      list.replaceChildren();
      for (const warning of warnings) {
        const item = document.createElement("li");
        item.textContent = warning;
        list.appendChild(item);
      }
      list.hidden = warnings.length === 0;
    }

    function render(status) {
      latest = status;
      setText("host", status.hostname);
      setText("summary", status.summary);
      setText("branch", status.repository.branch);
      // 表示文字列はサーバが組む（読めないホストでは空文字 → "---"）
      setText("head", status.repository.head_label);
      setText("upstream", status.repository.upstream);
      setText("notice", status.restart_notice);
      setNote("blocker", status.blocker ?? status.fetch_error ?? "");

      el("check").disabled = !status.enabled;
      el("run").disabled = !status.update_available || watching !== null;
      panel.querySelector(".update-actions").hidden = !status.enabled;

      const run = status.run;
      el("run-state").hidden = run.state === "idle";
      setText("state-label", run.state_label);
      setNote("error", run.error);
      renderSteps(run);
      renderWarnings(run);
    }

    async function refresh() {
      render(await call("GET", statusUrl));
    }

    // 更新後の復帰判定は、サーバが永続化した report で行う（再起動後も読める）。
    // run_id が一致し、リポジトリが目標 commit に到達していれば、サーバが復帰したと判定する。
    function returned(status, runId, targetHead) {
      return (
        status.run.run_id === runId &&
        Boolean(targetHead) &&
        status.repository.head === targetHead
      );
    }

    function pollOnce(runId) {
      // 再起動中は接続拒否が正常系。指数バックオフで問い合わせを続ける
      const backoff = createBackoff(500, 3000);
      const deadline = Date.now() + RECOVERY_LIMIT_MS;
      let targetHead = null;

      const tick = async () => {
        let status = null;
        try {
          status = await call("GET", statusUrl);
        } catch {
          // 再起動でサーバが停止している間の接続拒否。何もせず次の周期を待つ
        }
        if (status) {
          render(status);
          targetHead = status.run.to_head ?? targetHead;
          if (status.run.state === "failed") {
            watching = null;
            render(status);
            notify(`更新に失敗しました: ${status.run.error}`, false);
            return;
          }
          if (status.run.state === "succeeded" || returned(status, runId, targetHead)) {
            watching = null;
            render(status);
            notify("更新が完了しました");
            return;
          }
        }
        if (Date.now() > deadline) {
          watching = null;
          notify(
            "再起動後の復帰を確認できませんでした。" +
              "ssh して journalctl -u pcbasm-ui -n 200 を確認してください。",
            false
          );
          return;
        }
        const wait = latest && latest.run.state === "restarting" ? backoff.next() : 1000;
        watching = setTimeout(tick, wait);
      };

      watching = setTimeout(tick, 1000);
    }

    el("check").addEventListener("click", async () => {
      const button = el("check");
      button.disabled = true;
      try {
        render(await call("POST", checkUrl));
      } catch (err) {
        notify(`更新の確認に失敗しました: ${err.message}`, false);
      } finally {
        button.disabled = false;
      }
    });

    el("run").addEventListener("click", async () => {
      if (!latest) return;
      // 文言はサーバが組んだものをそのまま出す（再起動範囲の判断を JS に複製しない）
      if (!window.confirm(`${latest.hostname}\n${latest.restart_notice}\n実行しますか？`)) {
        return;
      }
      const button = el("run");
      button.disabled = true;
      try {
        const accepted = await call("POST", runUrl, {
          expected_head: latest.repository.head,
        });
        pollOnce(accepted.run_id);
        await refresh();
      } catch (err) {
        button.disabled = false;
        notify(`更新を開始できませんでした: ${err.message}`, false);
      }
    });

    refresh().catch((err) => notify(`更新状態を取得できません: ${err.message}`, false));
  }

  document.querySelectorAll("[data-update-panel]").forEach(setupPanel);
})();
