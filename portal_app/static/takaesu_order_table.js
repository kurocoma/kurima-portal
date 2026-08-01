// 高江洲発注表（編集・メール貼り付け用）の操作スクリプト。
// fragment は遅延ロード（outerHTML 差し替え）で挿入されるため、
// document へのイベント委譲で動かす（data-ot-* 属性が契約）。
// 保存は urlencoded（サーバーの _read_form は parse_qs。multipart は読めない）。
(() => {
  const SAVE_URL = "/inventory/takaesu/order-table/save";
  const RESET_URL = "/inventory/takaesu/order-table/reset";
  const SAVE_DEBOUNCE_MS = 800;
  // メール貼り付け用のコピー対象（2026-08-02 依頼: この3列のみ）
  const COPY_COLUMNS = ["仕入先CD", "商品名", "発注数"];
  const COL_WIDTH_STORAGE_KEY = "takaesu-order-table-col-widths";
  // 文字が列幅に収まらないときの縮小段階（Excelの「縮小して全体を表示」相当）
  const FIT_FONT_SIZES_PX = [12.5, 11.5, 10.5, 9.5];

  let saveTimer = null;
  let saving = false;
  let pendingSave = false;

  const root = () => document.getElementById("takaesu-order-table");
  const statusEl = () => root() ? root().querySelector("[data-ot-status]") : null;

  function setStatus(text, isError) {
    const el = statusEl();
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("is-error", Boolean(isError));
  }

  function columns() {
    const container = root();
    if (!container) return [];
    return Array.from(container.querySelectorAll("thead th"))
      .map((th) => th.textContent.trim())
      .filter(Boolean);
  }

  function collectRows() {
    const container = root();
    if (!container) return [];
    return Array.from(container.querySelectorAll("tbody tr")).map((tr) => {
      const row = {};
      for (const input of tr.querySelectorAll("input[data-ot-cell]")) {
        row[input.getAttribute("data-ot-cell")] = input.value;
      }
      return row;
    });
  }

  function scheduleSave() {
    setStatus("保存中…", false);
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, SAVE_DEBOUNCE_MS);
  }

  async function saveNow() {
    if (saving) {
      pendingSave = true;
      return;
    }
    saving = true;
    try {
      const body = new URLSearchParams({ rows: JSON.stringify(collectRows()) });
      const res = await fetch(SAVE_URL, { method: "POST", body });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const at = (data.saved_at || "").replace("T", " ");
      setStatus("保存済み" + (at ? "（" + at + "）" : ""), false);
    } catch (error) {
      setStatus("保存に失敗しました。通信を確認してもう一度編集してください。", true);
    } finally {
      saving = false;
      if (pendingSave) {
        pendingSave = false;
        saveNow();
      }
    }
  }

  function addRow() {
    const container = root();
    if (!container) return;
    const tbody = container.querySelector("tbody");
    if (!tbody) return;
    const tr = document.createElement("tr");
    for (const column of columns()) {
      const td = document.createElement("td");
      if (column === "発注数" || column === "受注数") td.className = "ot-qty";
      const input = document.createElement("input");
      input.type = "text";
      input.setAttribute("data-ot-cell", column);
      input.value = "";
      td.appendChild(input);
      tr.appendChild(td);
    }
    const delTd = document.createElement("td");
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "ot-del";
    delBtn.setAttribute("data-ot-del", "");
    delBtn.title = "この行を削除";
    delBtn.textContent = "✕";
    delTd.appendChild(delBtn);
    tr.appendChild(delTd);
    tbody.appendChild(tr);
    const firstInput = tr.querySelector("input");
    if (firstInput) firstInput.focus();
    scheduleSave();
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function copyTable(button) {
    const cols = COPY_COLUMNS;
    const rows = collectRows();
    const tsv = [
      cols.join("\t"),
      ...rows.map((row) => cols.map((column) => String(row[column] || "").replace(/[\t\r\n]+/g, " ")).join("\t")),
    ].join("\r\n");

    // メール本文に表として貼れるよう、罫線付きのインラインスタイルで組む
    const cellStyle = "border:1px solid #999;padding:3px 8px;font-size:13px;";
    const html =
      '<table style="border-collapse:collapse;border:1px solid #999;">' +
      "<tr>" + cols.map((column) => '<th style="' + cellStyle + 'background:#f0ede8;">' + escapeHtml(column) + "</th>").join("") + "</tr>" +
      rows.map((row) =>
        "<tr>" + cols.map((column) => {
          const numeric = column === "発注数" || column === "受注数";
          return '<td style="' + cellStyle + (numeric ? "text-align:right;" : "") + '">' + escapeHtml(row[column] || "") + "</td>";
        }).join("") + "</tr>"
      ).join("") +
      "</table>";

    try {
      if (navigator.clipboard && window.ClipboardItem) {
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/html": new Blob([html], { type: "text/html" }),
            "text/plain": new Blob([tsv], { type: "text/plain" }),
          }),
        ]);
      } else {
        await navigator.clipboard.writeText(tsv);
      }
      setStatus("表をコピーしました。メールやExcelに貼り付けできます。", false);
      if (button) {
        const original = button.textContent;
        button.textContent = "コピーしました";
        setTimeout(() => { button.textContent = original; }, 1600);
      }
    } catch (error) {
      setStatus("コピーに失敗しました。ブラウザの権限を確認してください。", true);
    }
  }

  async function resetTable() {
    if (!window.confirm("編集内容を破棄して、最新の高江洲発注書から表を作り直します。よろしいですか？")) {
      return;
    }
    setStatus("作り直しています…", false);
    try {
      const res = await fetch(RESET_URL, { method: "POST" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const html = await res.text();
      const container = root();
      if (container) container.outerHTML = html;
    } catch (error) {
      setStatus("作り直しに失敗しました。ページを再読込してください。", true);
    }
  }

  // ---- 商品名などの「縮小して全体を表示」と列幅の手動変更 ----

  function fitFont(input) {
    if (!(input instanceof HTMLInputElement)) return;
    input.style.fontSize = "";
    input.title = input.value;  // 縮小しても読みにくい場合はホバーで全文表示
    if (input.clientWidth === 0) return;
    for (const size of FIT_FONT_SIZES_PX) {
      if (input.scrollWidth <= input.clientWidth) break;
      input.style.fontSize = size + "px";
    }
  }

  function fitAllFonts(container) {
    for (const input of container.querySelectorAll("input[data-ot-cell]")) {
      fitFont(input);
    }
  }

  function fitColumnFonts(container, column) {
    for (const input of container.querySelectorAll(`input[data-ot-cell="${column}"]`)) {
      fitFont(input);
    }
  }

  function loadColWidths() {
    try {
      return JSON.parse(localStorage.getItem(COL_WIDTH_STORAGE_KEY) || "{}") || {};
    } catch (error) {
      return {};
    }
  }

  let widthSaveTimer = null;

  function initTable(container) {
    // 列幅の復元（端末ごとに localStorage 保存）→ 監視開始 → 文字サイズ調整
    const saved = loadColWidths();
    const headers = container.querySelectorAll("th[data-ot-col]");
    for (const th of headers) {
      const column = th.getAttribute("data-ot-col");
      if (saved[column]) th.style.width = saved[column] + "px";
    }
    fitAllFonts(container);

    const observer = new ResizeObserver((entries) => {
      const widths = loadColWidths();
      for (const entry of entries) {
        const column = entry.target.getAttribute("data-ot-col");
        if (!column) continue;
        widths[column] = Math.round(entry.target.getBoundingClientRect().width);
        fitColumnFonts(container, column);
      }
      if (widthSaveTimer) clearTimeout(widthSaveTimer);
      widthSaveTimer = setTimeout(() => {
        try {
          localStorage.setItem(COL_WIDTH_STORAGE_KEY, JSON.stringify(widths));
        } catch (error) { /* private mode 等では保存しない */ }
      }, 400);
    });
    for (const th of headers) observer.observe(th);
  }

  function saveColumnWidth(th) {
    const column = th.getAttribute("data-ot-col");
    if (!column) return;
    const widths = loadColWidths();
    widths[column] = Math.round(th.getBoundingClientRect().width);
    try {
      localStorage.setItem(COL_WIDTH_STORAGE_KEY, JSON.stringify(widths));
    } catch (error) { /* private mode 等では保存しない */ }
  }

  // 列幅ドラッグの確定はドラッグ終了(mouseup)でも処理する。ResizeObserver は
  // レンダリングフレームに依存し、バックグラウンドタブでは発火しないため二重化。
  document.addEventListener("mouseup", (event) => {
    const th = event.target instanceof HTMLElement ? event.target.closest("th[data-ot-col]") : null;
    if (!th) return;
    const container = th.closest("[data-order-table]");
    if (!container) return;
    fitColumnFonts(container, th.getAttribute("data-ot-col"));
    saveColumnWidth(th);
  });

  // バックグラウンドで描画された表は、表示された時点で文字サイズを合わせ直す
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    const container = root();
    if (container) fitAllFonts(container);
  });

  // fragment は遅延ロード/作り直しで後から挿入されるため、出現を監視して初期化する
  const appearanceObserver = new MutationObserver(() => {
    const container = root();
    if (container && container.hasAttribute("data-order-table") && !container.dataset.otInit) {
      container.dataset.otInit = "1";
      initTable(container);
    }
  });
  appearanceObserver.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", () => {
    const container = root();
    if (container && container.hasAttribute("data-order-table") && !container.dataset.otInit) {
      container.dataset.otInit = "1";
      initTable(container);
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target instanceof HTMLElement && event.target.closest("[data-order-table]")) {
      if (event.target instanceof HTMLInputElement) fitFont(event.target);
      scheduleSave();
    }
  });

  // 数量セルはフォーカスで既存値を全選択し、そのまま数値を打てば上書きされるようにする
  // （「値を消してから入力」の手間をなくす）。クリック直後の mouseup が選択を解除して
  // しまうため、フォーカス直後の1回だけ既定動作を抑止する。
  // 全セルで Esc 取り消し用にフォーカス時点の値も覚えておく。
  document.addEventListener("focusin", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    if (!input.closest("[data-order-table]")) return;
    if (!input.hasAttribute("data-ot-cell")) return;
    input.dataset.otPrev = input.value;
    const column = input.getAttribute("data-ot-cell");
    if (column !== "発注数" && column !== "受注数") return;
    input.select();
    input.addEventListener("mouseup", (mouseEvent) => mouseEvent.preventDefault(), { once: true });
  });

  // Excel風のキーボード移動:
  //   ↑/↓        … 上下のセルへ（移動先は全選択）
  //   Enter       … 下のセルへ（Shift+Enter は上へ）
  //   ←/→        … カーソルが端（または全選択状態）のときだけ隣のセルへ。
  //                  文字の途中では通常のカーソル移動のまま。
  //   Esc         … フォーカス時点の値に戻す
  function moveFocus(input, rowDelta, colDelta) {
    const tr = input.closest("tr");
    const tbody = tr ? tr.parentElement : null;
    if (!tr || !tbody) return false;
    const cellsInRow = Array.from(tr.querySelectorAll("input[data-ot-cell]"));
    const colIndex = cellsInRow.indexOf(input);
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const rowIndex = rows.indexOf(tr);
    const targetRow = rowIndex + rowDelta;
    const targetCol = colIndex + colDelta;
    if (targetRow < 0 || targetRow >= rows.length) return false;
    const targetCells = Array.from(rows[targetRow].querySelectorAll("input[data-ot-cell]"));
    if (targetCol < 0 || targetCol >= targetCells.length) return false;
    const target = targetCells[targetCol];
    target.focus();
    target.select();
    return true;
  }

  document.addEventListener("keydown", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    if (!input.closest("[data-order-table]")) return;
    if (!input.hasAttribute("data-ot-cell")) return;

    if (event.key === "Escape") {
      if (input.dataset.otPrev !== undefined && input.dataset.otPrev !== input.value) {
        input.value = input.dataset.otPrev;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      input.select();
      event.preventDefault();
      return;
    }

    let rowDelta = 0;
    let colDelta = 0;
    if (event.key === "ArrowUp") rowDelta = -1;
    else if (event.key === "ArrowDown") rowDelta = 1;
    else if (event.key === "Enter") rowDelta = event.shiftKey ? -1 : 1;
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const length = input.value.length;
      const allSelected = length > 0 && input.selectionStart === 0 && input.selectionEnd === length;
      const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
      const atEnd = input.selectionStart === length && input.selectionEnd === length;
      if (event.key === "ArrowLeft" && (allSelected || atStart || length === 0)) colDelta = -1;
      else if (event.key === "ArrowRight" && (allSelected || atEnd || length === 0)) colDelta = 1;
      else return;
    } else {
      return;
    }

    if (moveFocus(input, rowDelta, colDelta)) {
      event.preventDefault();
    } else if (event.key === "Enter") {
      event.preventDefault();
    }
  });

  document.addEventListener("click", (event) => {
    if (!(event.target instanceof HTMLElement)) return;
    const container = event.target.closest("[data-order-table]");
    if (!container) return;
    const del = event.target.closest("[data-ot-del]");
    if (del) {
      const tr = del.closest("tr");
      if (tr) tr.remove();
      scheduleSave();
      return;
    }
    if (event.target.closest("[data-ot-add]")) {
      addRow();
      return;
    }
    const copyButton = event.target.closest("[data-ot-copy]");
    if (copyButton) {
      copyTable(copyButton);
      return;
    }
    if (event.target.closest("[data-ot-reset]")) {
      resetTable();
    }
  });
})();
