// 高江洲発注表（編集・メール貼り付け用）の操作スクリプト。
// fragment は遅延ロード（outerHTML 差し替え）で挿入されるため、
// document へのイベント委譲で動かす（data-ot-* 属性が契約）。
// 保存は urlencoded（サーバーの _read_form は parse_qs。multipart は読めない）。
(() => {
  const SAVE_URL = "/inventory/takaesu/order-table/save";
  const RESET_URL = "/inventory/takaesu/order-table/reset";
  const SAVE_DEBOUNCE_MS = 800;

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
    const cols = columns();
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

  document.addEventListener("input", (event) => {
    if (event.target instanceof HTMLElement && event.target.closest("[data-order-table]")) {
      scheduleSave();
    }
  });

  // 数量セルはフォーカスで既存値を全選択し、そのまま数値を打てば上書きされるようにする
  // （「値を消してから入力」の手間をなくす）。クリック直後の mouseup が選択を解除して
  // しまうため、フォーカス直後の1回だけ既定動作を抑止する。
  document.addEventListener("focusin", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    if (!input.closest("[data-order-table]")) return;
    const column = input.getAttribute("data-ot-cell");
    if (column !== "発注数" && column !== "受注数") return;
    input.select();
    input.addEventListener("mouseup", (mouseEvent) => mouseEvent.preventDefault(), { once: true });
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
