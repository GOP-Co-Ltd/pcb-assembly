"use strict";

// ソフトウェア更新パネル（backend 側 /dev/update と frontend 自身の /update で共用）。
// エンドポイントとトランスポートはルート要素の data-* で切り替える。
//
// このファイルはロジックを持たない。「同居機なので画面も切れます」等の文言、状態の
// 要約、手順のラベルはすべてサーバが status API で返した文字列をそのまま描く。
(() => {
  const panel = document.getElementById("update-panel");
  if (!panel) return;

  const { statusUrl, checkUrl, runUrl, transport } = panel.dataset;
  const { api, frontendApi, toast, createBackoff } = window.webui;
  // backend 側は api()（machine prefix が付き、423 で control.onDenied が効く）。
  // frontend 自身のエンドポイントは prefix を付けずに叩く。
  const call = (method, url, body) =>
    transport === "frontend"
      ? frontendApi(method, url, body)
      : api(method, url, body);

  // 再起動を跨いだ復帰待ちの上限。超えたら諦めて調べ方を出す
  const RECOVERY_LIMIT_MS = 180000;

  const el = (id) => document.getElementById(id);
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
    const list = el("update-steps");
    list.replaceChildren();
    for (const step of run.steps ?? []) {
      const item = document.createElement("li");
      item.className = step.ok ? "ok" : "ng";
      item.textContent = step.label;
      list.appendChild(item);
    }
    // 失敗した手順の出力はサーバが選んで返す（steps から JS で再導出しない）
    setNote("update-detail", run.failed_detail ?? "");
  }

  function renderWarnings(run) {
    const list = el("update-warnings");
    // 版ずれした backend が返さないフィールドで TypeError を投げない
    // （pollOnce の try の外なので、投げるとポーリングが無言で止まる）
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
    setText("update-host", status.hostname);
    setText("update-summary", status.summary);
    setText("update-branch", status.repository.branch);
    // 表示文字列はサーバが組む（読めないホストでは空文字 → "---"）
    setText("update-head", status.repository.head_label);
    setText("update-upstream", status.repository.upstream);
    setText("update-notice", status.restart_notice);
    setNote("update-blocker", status.blocker ?? status.fetch_error ?? "");

    el("update-check").disabled = !status.enabled;
    el("update-run").disabled = !status.update_available || watching !== null;
    panel.querySelector(".update-actions").hidden = !status.enabled;

    const run = status.run;
    el("update-run-state").hidden = run.state === "idle";
    setText("update-state-label", run.state_label);
    setNote("update-error", run.error);
    renderSteps(run);
    renderWarnings(run);
  }

  async function refresh() {
    render(await call("GET", statusUrl));
  }

  // 更新後の復帰判定はサーバが永続化した report で行う（再起動を跨いで読める）:
  // run_id が一致し、リポジトリが目標 commit に到達していれば「戻ってきた」。
  function returned(status, runId, targetHead) {
    return (
      status.run.run_id === runId &&
      Boolean(targetHead) &&
      status.repository.head === targetHead
    );
  }

  function pollOnce(runId) {
    // 再起動中は接続拒否が正常系。指数バックオフで叩き続ける
    const backoff = createBackoff(500, 3000);
    const deadline = Date.now() + RECOVERY_LIMIT_MS;
    let targetHead = null;

    const tick = async () => {
      let status = null;
      try {
        status = await call("GET", statusUrl);
      } catch {
        // 再起動で落ちている間の接続拒否。何もせず次の周期を待つ
      }
      if (status) {
        render(status);
        targetHead = status.run.to_head ?? targetHead;
        if (status.run.state === "failed") {
          watching = null;
          render(status);
          toast(`更新に失敗しました: ${status.run.error}`, false);
          return;
        }
        if (status.run.state === "succeeded" || returned(status, runId, targetHead)) {
          watching = null;
          render(status);
          toast("更新が完了しました");
          return;
        }
      }
      if (Date.now() > deadline) {
        watching = null;
        toast(
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

  el("update-check").addEventListener("click", async () => {
    const button = el("update-check");
    button.disabled = true;
    try {
      render(await call("POST", checkUrl));
    } catch (err) {
      toast(`更新の確認に失敗しました: ${err.message}`, false);
    } finally {
      button.disabled = false;
    }
  });

  el("update-run").addEventListener("click", async () => {
    if (!latest) return;
    // 文言はサーバが組んだものをそのまま出す（再起動範囲の判断を JS に複製しない）
    if (!window.confirm(`${latest.hostname}\n${latest.restart_notice}\n実行しますか？`)) {
      return;
    }
    const button = el("update-run");
    button.disabled = true;
    try {
      const accepted = await call("POST", runUrl, {
        expected_head: latest.repository.head,
      });
      pollOnce(accepted.run_id);
      await refresh();
    } catch (err) {
      button.disabled = false;
      toast(`更新を開始できませんでした: ${err.message}`, false);
    }
  });

  refresh().catch((err) => toast(`更新状態を取得できません: ${err.message}`, false));
})();
