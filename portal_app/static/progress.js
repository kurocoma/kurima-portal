/* くりまポータル 共通進捗スクリプト（U7: 進捗JSの共通化＋完了通知 / U4: 再アタッチ）
 *
 * base.html から全ページで読み込まれ、2つの役割を持つ。
 *   1) ナビ「実行履歴」の実行中バッジ（全ページ・/progress/active を10秒ごとに確認）
 *   2) 進捗パネルのポーリング表示（ジョブ実行ページのみ）
 *
 * 各ページはコンテンツ側の <script> で window.KURIMA_PROGRESS に設定を入れる:
 *   workflows:         このページが扱う workflow 名の配列（再アタッチ対象。U4）
 *   startTitle:        送信直後に出す仮タイトル（既定 "処理状況"）
 *   stepChips:         ステップkey → フローチップ番号（数値 or 配列）の対応表
 *   completeRedirect:  完了時の遷移先URL。"reload" で現在URLの再読込。無指定で遷移なし
 *   completeRedirectSkip: 完了時リダイレクトを行わない workflow 名の配列
 *                      （対象チェック等、結果メッセージを画面に残したいジョブ用）
 *   completeMessage:   完了時に message へ上書きする文言
 *   settingsShareKeys: data-settings-from 参照時に共有するフォームキー（ヤマト一括実行）
 *   renderResult:      (job, resultEl) => void  結果表示の描画（クリックポスト/出荷確定）
 *   beforeSubmit:      (form) => string|null    送信前の検証＋hidden反映。文字列を返すと中断
 *   jobKind:           (form) => string|null    ジョブ種別の記録（出荷確定の fetch 判定）
 *   onJobDone:         (job, kind) => void      終了時フック（出荷確定の自動マッピング再読込）
 *   afterEnableButtons:() => void               ボタン再有効化後のフック（upload ボタン状態の復元）
 */
(() => {
  "use strict";

  // ---------- ナビ「実行履歴」の実行中バッジ（U4・全ページ） ----------
  const refreshRunningBadge = async () => {
    const badge = document.getElementById("nav-running-badge");
    if (!badge) return;
    try {
      const response = await fetch("/progress/active", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      const count = payload.count || 0;
      badge.hidden = count === 0;
      badge.textContent = String(count);
    } catch (error) { /* バッジは補助表示。取得失敗で画面を壊さない */ }
  };
  refreshRunningBadge();
  setInterval(refreshRunningBadge, 10000);

  // ---------- 完了通知（U7: タブタイトル・favicon・音・デスクトップ通知） ----------
  const baseTitle = document.title;
  const STATUS_PREFIX = { completed: "〔完了〕", failed: "〔エラー〕", cancelled: "〔中止〕" };

  const setFaviconBadge = (color) => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 32;
      const ctx = canvas.getContext("2d");
      ctx.beginPath();
      ctx.arc(16, 16, 12, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      let link = document.querySelector('link[rel="icon"]');
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        link.dataset.kurimaBadge = "1";
        document.head.appendChild(link);
      }
      link.href = canvas.toDataURL("image/png");
    } catch (error) { /* favicon は装飾のみ */ }
  };
  const clearFaviconBadge = () => {
    const link = document.querySelector('link[rel="icon"][data-kurima-badge]');
    if (link) link.remove();
  };

  // 短い通知音（Web Audio）。AudioContext はジョブ開始のクリック（ユーザー操作）時に
  // 用意しておく（自動再生制限対策）。用意できない環境では黙ってスキップする。
  let audioCtx = null;
  const ensureAudio = () => {
    try {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!audioCtx && Ctor) audioCtx = new Ctor();
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
    } catch (error) { audioCtx = null; }
  };
  const beep = (ok) => {
    try {
      if (!audioCtx || audioCtx.state !== "running") return;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = ok ? 880 : 330;
      gain.gain.setValueAtTime(0.08, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.5);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + 0.5);
    } catch (error) { /* 音は補助。失敗しても何もしない */ }
  };

  // デスクトップ通知は secure context（localhost / https）のみ試行する。
  // LAN の http では isSecureContext=false のため自動的にスキップされる
  // （その場合もタイトル・favicon・音の通知は動く）。許可はジョブ初回開始時に求める。
  const canDesktopNotify = () => window.isSecureContext && "Notification" in window;
  const maybeRequestNotifyPermission = () => {
    try {
      if (canDesktopNotify() && Notification.permission === "default") {
        Notification.requestPermission().catch(() => {});
      }
    } catch (error) { /* 通知は補助 */ }
  };
  const desktopNotify = (title, body) => {
    try {
      if (canDesktopNotify() && Notification.permission === "granted") {
        new Notification(title, { body: body || "" });
      }
    } catch (error) { /* 通知は補助 */ }
  };

  const notifyJobDone = (job) => {
    const prefix = STATUS_PREFIX[job.status];
    if (!prefix) return;
    document.title = prefix + baseTitle;
    setFaviconBadge(job.status === "completed" ? "#1c7f4b" : "#b3261e");
    beep(job.status === "completed");
    desktopNotify(prefix + (job.title || "くりまポータル"), job.message);
    // 画面を見ているときは 5 秒で元に戻す（別タブ作業中はタブへ戻った時に戻す）
    if (!document.hidden) {
      setTimeout(() => { document.title = baseTitle; clearFaviconBadge(); }, 5000);
    }
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      document.title = baseTitle;
      clearFaviconBadge();
    }
  });

  // ---------- 進捗パネル（設定 window.KURIMA_PROGRESS があるページのみ） ----------
  const config = window.KURIMA_PROGRESS;
  if (!config) return;
  const panel = document.getElementById("progress-panel");
  if (!panel) return;

  const title = document.getElementById("progress-title");
  const message = document.getElementById("progress-message");
  const statusBadge = document.getElementById("progress-status");
  const stepsList = document.getElementById("progress-steps");
  const errorBox = document.getElementById("progress-error");
  const hintBox = document.getElementById("progress-hint");
  const resultEl = document.getElementById("progress-result");
  const cancelBtn = document.getElementById("progress-cancel");
  let currentJobId = null;
  let currentJobKind = null;

  const statusLabels = { queued: "待機中", running: "処理中", completed: "完了", failed: "エラー", cancelled: "中止", pending: "待機中" };
  const stepLabels = { pending: "待機中", running: "処理中", completed: "完了", failed: "エラー" };

  // 進捗ステップ key を上部フローチップへ対応付け、実行中/完了/失敗を表示する。
  const flowChips = [...document.querySelectorAll(".flow [data-chip]")];
  const chipNumbersFor = (stepKey) => {
    const value = (config.stepChips || {})[stepKey];
    if (Array.isArray(value)) return value;
    return value ? [value] : [];
  };
  const clearFlowChips = () => {
    for (const chip of flowChips) chip.classList.remove("is-active", "is-done", "is-failed");
  };
  const updateFlowChips = (job) => {
    if (!flowChips.length) return;
    const byChip = {};
    for (const step of job.steps || []) {
      for (const n of chipNumbersFor(step.key)) {
        (byChip[n] = byChip[n] || []).push(step.status);
      }
    }
    for (const chip of flowChips) {
      const n = Number(chip.dataset.chip);
      const st = byChip[n] || [];
      chip.classList.remove("is-active", "is-done", "is-failed");
      if (!st.length) continue;
      if (st.includes("failed")) chip.classList.add("is-failed");
      else if (st.includes("running")) chip.classList.add("is-active");
      else if (st.every((s) => s === "completed")) chip.classList.add("is-done");
      else if (st.includes("completed")) chip.classList.add("is-active");
    }
  };

  const setButtonsDisabled = (disabled) => {
    for (const button of document.querySelectorAll("[data-progress-form] button")) {
      button.disabled = disabled;
    }
    if (!disabled && config.afterEnableButtons) config.afterEnableButtons();
  };

  const render = (job) => {
    panel.hidden = false;
    panel.classList.toggle("ok", job.status === "completed");
    panel.classList.toggle("error", job.status === "failed" || job.status === "cancelled");
    panel.classList.toggle("warn", job.status === "running" || job.status === "queued");
    const active = job.status === "running" || job.status === "queued";
    if (cancelBtn) cancelBtn.hidden = !active;
    title.textContent = job.title || "処理状況";
    message.textContent = job.message || "";
    statusBadge.textContent = statusLabels[job.status] || job.status;
    stepsList.innerHTML = "";
    for (const step of job.steps || []) {
      const item = document.createElement("li");
      item.className = `progress-step is-${step.status}`;
      const dot = document.createElement("span");
      dot.className = "step-dot";
      const body = document.createElement("div");
      const label = document.createElement("strong");
      label.textContent = step.label;
      const detail = document.createElement("small");
      detail.textContent = `${stepLabels[step.status] || step.status}${step.detail ? " - " + step.detail : ""}`;
      body.append(label, detail);
      item.append(dot, body);
      stepsList.appendChild(item);
    }
    errorBox.hidden = !job.error;
    errorBox.textContent = job.error || "";
    // 失敗時は日本語の対処ガイドと「詳細ログを見る」リンクを添える（U1）
    hintBox.textContent = "";
    if (job.error) {
      if (job.hint) hintBox.append(job.hint + " ");
      if (job.log_events_rel) {
        const logLink = document.createElement("a");
        logLink.className = "text-link";
        logLink.href = "/logs/view?path=" + encodeURIComponent(job.log_events_rel);
        logLink.textContent = "詳細ログを見る";
        hintBox.append(logLink);
      }
    }
    hintBox.hidden = !hintBox.hasChildNodes();
    if (config.renderResult && resultEl) config.renderResult(job, resultEl);
    updateFlowChips(job);
  };

  const resetCancelButton = () => {
    if (!cancelBtn) return;
    cancelBtn.hidden = true;
    cancelBtn.disabled = false;
    cancelBtn.textContent = "実行を中止";
  };

  const finishJob = (job) => {
    notifyJobDone(job);
    refreshRunningBadge();
    resetCancelButton();
    const kind = currentJobKind;
    currentJobKind = null;
    if (
      job.status === "completed" &&
      config.completeRedirect &&
      !(config.completeRedirectSkip || []).includes(job.workflow)
    ) {
      if (config.completeMessage) message.textContent = config.completeMessage;
      setTimeout(() => {
        window.location.assign(
          config.completeRedirect === "reload" ? window.location.href : config.completeRedirect
        );
      }, 900);
      return;
    }
    setButtonsDisabled(false);
    if (config.onJobDone) {
      Promise.resolve(config.onJobDone(job, kind)).catch(() => {});
    }
  };

  const poll = async (jobId) => {
    const response = await fetch(`/progress/${jobId}`, { cache: "no-store" });
    if (!response.ok) throw new Error("進捗を取得できませんでした。");
    const job = await response.json();
    render(job);
    if (job.status === "completed" || job.status === "failed" || job.status === "cancelled") {
      finishJob(job);
      return;
    }
    setTimeout(() => poll(jobId).catch(showError), 1000);
  };

  const showError = (error) => {
    clearFlowChips();
    panel.hidden = false;
    panel.classList.remove("ok", "warn");
    panel.classList.add("error");
    title.textContent = "処理状況";
    message.textContent = "進捗表示でエラーが発生しました。";
    statusBadge.textContent = "エラー";
    errorBox.hidden = false;
    errorBox.textContent = (error && error.message) || String(error);
    hintBox.hidden = true;
    hintBox.textContent = "";
    setButtonsDisabled(false);
  };

  for (const form of document.querySelectorAll("[data-progress-form]")) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const endpoint = form.dataset.progressEndpoint;
      if (!endpoint) { form.submit(); return; }
      // ページ固有の検証＋hidden反映（出荷確定）。文字列が返ったら中断してエラー表示。
      if (config.beforeSubmit) {
        const blocked = config.beforeSubmit(form);
        if (blocked) { showError(new Error(blocked)); return; }
      }
      // フォーム値（headed 等の checkbox や event.submitter の値）を漏れなく含めるため、
      // ボタンを無効化する「前」に本文を構築する（無効化された control は entry list から除外される）。
      // さらに fetch に FormData を渡すと multipart になりサーバーの parse_qs が読めないので、
      // URLSearchParams で urlencoded 送信する。
      const body = new URLSearchParams(new FormData(form, event.submitter));
      // data-settings-from を持つフォーム（ヤマト一括実行）は、参照先フォームの
      // 詳細設定（テストモード・ブラウザ表示など settingsShareKeys のキー）を共有する。
      const settingsFormId = form.dataset.settingsFrom;
      if (settingsFormId && config.settingsShareKeys) {
        const settingsForm = document.getElementById(settingsFormId);
        if (settingsForm) {
          const shared = new FormData(settingsForm);
          for (const key of config.settingsShareKeys) {
            if (shared.has(key) && !body.has(key)) body.append(key, shared.get(key));
          }
        }
      }
      ensureAudio();
      maybeRequestNotifyPermission();
      setButtonsDisabled(true);
      clearFlowChips();
      render({ title: config.startTitle || "処理状況", status: "queued", message: "ジョブを開始しています。", steps: [] });
      try {
        const response = await fetch(endpoint, { method: "POST", body });
        if (!response.ok) {
          // 409（二重実行ガード）等の日本語メッセージ本文をそのまま表示する
          const text = await response.text().catch(() => "");
          throw new Error(text || "処理を開始できませんでした。");
        }
        const payload = await response.json();
        currentJobId = payload.job_id;
        currentJobKind = config.jobKind ? config.jobKind(form) : null;
        if (cancelBtn) { cancelBtn.hidden = false; cancelBtn.disabled = false; cancelBtn.textContent = "実行を中止"; }
        await poll(payload.job_id);
      } catch (error) {
        showError(error);
      }
    });
  }

  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      if (!currentJobId) return;
      cancelBtn.disabled = true;
      cancelBtn.textContent = "中止しています…";
      try {
        await fetch(`/progress/${currentJobId}/cancel`, { method: "POST" });
      } catch (error) { /* ポーリングが中止状態を拾う */ }
    });
  }

  // ---------- 実行中ジョブへの再アタッチ（U4） ----------
  // ページ再訪・リロード時に、このページの workflow の実行中ジョブがあれば
  // 進捗パネルへ自動で再接続する（「消えたように見える」と二度押しの防止）。
  const reattach = async () => {
    if (!config.workflows || !config.workflows.length || currentJobId) return;
    try {
      const response = await fetch("/progress/active", { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      const mine = (payload.jobs || []).find((job) => (config.workflows || []).includes(job.workflow));
      if (!mine || currentJobId) return;
      currentJobId = mine.job_id;
      setButtonsDisabled(true);
      if (cancelBtn) { cancelBtn.hidden = false; cancelBtn.disabled = false; cancelBtn.textContent = "実行を中止"; }
      await poll(mine.job_id);
    } catch (error) {
      // 再接続の失敗（直後のサーバー再起動等）は通常表示のまま（本体機能を巻き込まない）
      setButtonsDisabled(false);
      resetCancelButton();
    }
  };
  reattach();
})();
