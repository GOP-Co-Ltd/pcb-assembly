"use strict";

// 入力の遅延保存と進行中の通信をまとめ、ジョブ開始前に完了を待つ。
export function createPendingSaves(reportError) {
  const scheduled = new Map();
  const running = new Set();

  function cancel(key) {
    clearTimeout(scheduled.get(key)?.timer);
    scheduled.delete(key);
  }

  function schedule(key, save, delay) {
    cancel(key);
    const flush = () => {
      cancel(key);
      save();
    };
    scheduled.set(key, { flush, timer: setTimeout(flush, delay) });
  }

  function run(label, save) {
    const result = Promise.resolve().then(save).then(
      () => null,
      (error) => {
        const failure = new Error(`${label}: ${error.message}`);
        reportError(failure);
        return failure;
      }
    );
    running.add(result);
    result.then(() => running.delete(result));
    return result;
  }

  async function flush() {
    let failure = null;
    while (scheduled.size || running.size) {
      const pending = new Set(running);
      for (const entry of [...scheduled.values()]) entry.flush();
      for (const result of running) pending.add(result);
      for (const error of await Promise.all(pending)) failure ??= error;
    }
    return failure;
  }

  return { cancel, schedule, run, flush };
}
