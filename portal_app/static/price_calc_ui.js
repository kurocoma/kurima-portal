/* =========================================================
   税込・割引価格計算（楽天RMS向け） — 画面ロジック
   計算は price_calc.js（PriceCalc）に委譲し、この層は
   行の管理・表示・コピー・CSV出力・一時保存のみを担う。

   - 自動計算（FR-004）: 入力イベントごとに該当行のみ再計算する。
     入力欄は再描画しないため、入力中のフォーカスは失われない。
   - 一時保存（FR-013）: localStorage に入力値のみ保存し、
     次回起動時に復元する。全消去で保存データも消す（NFR-006）。
   - コピー（FR-009）: 画面表示値ではなく内部の整数計算結果を使う（§8.2）。
   ========================================================= */
(function () {
  "use strict";

  var PC = window.PriceCalc;
  var STORAGE_KEY = "kurima_price_calc_v1";

  var body = document.getElementById("pc-body");
  var addBtn = document.getElementById("pc-add");
  var csvBtn = document.getElementById("pc-csv");
  var clearBtn = document.getElementById("pc-clear");
  var modal = document.getElementById("pc-modal");
  var modalOk = document.getElementById("pc-modal-ok");
  var modalCancel = document.getElementById("pc-modal-cancel");
  var csvNote = document.getElementById("pc-csv-note");

  var nextId = 1;
  var rows = []; // {id, product, unitNet, quantity, taxPct, discount, dom:{...}, result}

  /* ---------- 一時保存（FR-013） ---------- */

  var saveTimer = null;
  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveState, 300);
  }

  function saveState() {
    try {
      var data = rows.map(function (r) {
        return {
          product: r.product,
          unitNet: r.unitNet,
          quantity: r.quantity,
          taxPct: r.taxPct,
          discount: r.discount
        };
      });
      localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
      /* 保存不可でも計算機能は継続する（NFR-004） */
    }
  }

  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      if (!Array.isArray(data) || data.length === 0) return null;
      return data.map(function (d) {
        return {
          product: typeof d.product === "string" ? d.product : "",
          unitNet: typeof d.unitNet === "string" ? d.unitNet : "",
          quantity: typeof d.quantity === "string" ? d.quantity : "1",
          taxPct: d.taxPct === 8 ? 8 : 10,
          discount: typeof d.discount === "string" ? d.discount : "0.00"
        };
      });
    } catch (e) {
      return null;
    }
  }

  /* ---------- クリップボード（LAN配信のhttpでも動くようフォールバック付き） ---------- */

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) { /* fallthrough */ }
      ta.remove();
      if (ok) resolve(); else reject(new Error("コピーに失敗しました"));
    });
  }

  function flashButton(btn, text) {
    var original = btn.textContent;
    btn.textContent = text;
    btn.disabled = true;
    setTimeout(function () {
      btn.textContent = original;
      btn.disabled = false;
      updateRowButtons(findRowByDom(btn));
    }, 1200);
  }

  function findRowByDom(el) {
    var tr = el.closest("tr");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].dom.mainTr === tr || rows[i].dom.detailTr === tr) return rows[i];
    }
    return null;
  }

  /* ---------- Excel等からの表形式貼り付け ---------- */

  // 画面の入力列の並び。貼り付けはフォーカス中の欄を起点に、この順で右のセルへ割り当てる
  var FIELD_ORDER = ["product", "unitNet", "quantity", "taxPct", "discount"];

  /** 1セル分の値を行の指定フィールドへ反映する（表示は blur 時と同じ整形を適用） */
  function setField(row, field, raw) {
    var v = String(raw == null ? "" : raw).trim();
    var dom = row.dom;
    if (field === "product") {
      row.product = v;
      dom.productInput.value = v;
    } else if (field === "unitNet") {
      var p = PC.parseIntegerYen(v);
      row.unitNet = p != null && v !== "" ? p.toLocaleString("ja-JP") : v;
      dom.unitNetInput.value = row.unitNet;
    } else if (field === "quantity") {
      var q = PC.parseIntegerYen(v);
      row.quantity = q != null && v !== "" ? String(q) : v;
      dom.quantityInput.value = row.quantity;
    } else if (field === "taxPct") {
      // §8: 税率の不正値は受け付けない → 8/10 以外は現在の選択を維持する
      var t = PC.parseTaxPct(v);
      if (t != null) {
        row.taxPct = t;
        dom.taxSelect.value = String(t);
      }
    } else if (field === "discount") {
      var bp = PC.parseDiscountBp(v);
      row.discount = bp != null ? PC.formatDiscountBp(bp) : v;
      dom.discountInput.value = row.discount;
    }
  }

  /** 複数セルの貼り付けを展開する。起点行・起点列から右・下へ埋め、行が足りなければ追加する */
  function applyPaste(startRow, startField, matrix) {
    var startCol = FIELD_ORDER.indexOf(startField);
    var startIdx = rows.indexOf(startRow);
    if (startCol === -1 || startIdx === -1) return;
    var touched = [];
    matrix.forEach(function (cells, i) {
      var row = rows[startIdx + i] || addRow({});
      cells.forEach(function (cell, j) {
        var field = FIELD_ORDER[startCol + j];
        if (!field) return; // 列が余る分は無視する
        setField(row, field, cell);
      });
      touched.push(row);
    });
    touched.forEach(function (r) { recalcRow(r); });
    scheduleSave();
  }

  /** 入力欄に貼り付けリスナーを付ける。複数セルのときだけ表として展開する */
  function attachPaste(input, row, field) {
    input.addEventListener("paste", function (ev) {
      var data = ev.clipboardData || window.clipboardData;
      if (!data) return;
      var text = data.getData("text/plain") || data.getData("text");
      if (!PC.isTablePaste(text)) return; // 単一セルは通常の貼り付けに任せる
      ev.preventDefault();
      applyPaste(row, field, PC.parseClipboardTable(text));
    });
  }

  /* ---------- 行DOMの生成 ---------- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function inputCell(label, input) {
    var td = el("td", "pc-cell pc-cell-input");
    td.setAttribute("data-label", label);
    td.appendChild(input);
    var err = el("div", "pc-err");
    err.hidden = true;
    td.appendChild(err);
    return { td: td, err: err };
  }

  function resultCell(label, extraClass) {
    var td = el("td", "pc-cell num" + (extraClass ? " " + extraClass : ""));
    td.setAttribute("data-label", label);
    td.textContent = "—";
    return td;
  }

  function createRow(initial) {
    var row = {
      id: nextId++,
      product: initial.product || "",
      unitNet: initial.unitNet || "",
      quantity: initial.quantity || "1",
      taxPct: initial.taxPct === 8 ? 8 : 10,
      discount: initial.discount != null && initial.discount !== "" ? initial.discount : "0.00",
      result: null,
      status: "empty",
      dom: {}
    };

    var mainTr = el("tr", "pc-row");
    var detailTr = el("tr", "pc-detail-row");

    // 商品（IN-01）
    var productInput = el("input", "input pc-input-product");
    productInput.type = "text";
    productInput.maxLength = PC.LIMITS.productMaxLen;
    productInput.placeholder = "商品名・SKU管理番号など（任意）";
    productInput.value = row.product;
    var productCell = inputCell("商品", productInput);
    productCell.td.classList.add("pc-col-product");

    // 商品単価（税抜）（IN-02）。上下キーでの値変更を避けるため type=text + inputmode（§7.3）
    var unitNetInput = el("input", "input pc-input-unitnet");
    unitNetInput.type = "text";
    unitNetInput.inputMode = "numeric";
    unitNetInput.placeholder = "例: 3,565";
    unitNetInput.value = row.unitNet;
    var unitNetCell = inputCell("税抜単価", unitNetInput);

    // 数量（IN-03）
    var quantityInput = el("input", "input pc-input-quantity");
    quantityInput.type = "text";
    quantityInput.inputMode = "numeric";
    quantityInput.value = row.quantity;
    var quantityCell = inputCell("数量", quantityInput);

    // 税率（IN-04）
    var taxSelect = el("select", "select pc-input-tax");
    [10, 8].forEach(function (pct) {
      var opt = el("option", null, pct + "%");
      opt.value = String(pct);
      taxSelect.appendChild(opt);
    });
    taxSelect.value = String(row.taxPct);
    var taxCell = inputCell("税率", taxSelect);

    // 割引率（IN-05）
    var discountInput = el("input", "input pc-input-discount");
    discountInput.type = "text";
    discountInput.inputMode = "decimal";
    discountInput.value = row.discount;
    var discountCell = inputCell("割引率%", discountInput);

    // 結果セル（必須出力5項目 §5.2。並びはワイヤーフレーム §7.2 準拠）
    var taxCellOut = resultCell("税額");
    var beforeGrossCell = resultCell("割引前税込");
    var afterGrossCell = resultCell("割引後税込");
    var afterNetCell = resultCell("割引後税抜", "pc-rms");
    var perUnitCell = resultCell("1個当たり");

    // 操作
    var opsTd = el("td", "pc-cell pc-col-ops");
    opsTd.setAttribute("data-label", "操作");
    var copyBtn = el("button", "btn btn-sm pc-copy", "コピー");
    copyBtn.type = "button";
    copyBtn.title = "RMS登録用税抜価格をコピー";
    var dupBtn = el("button", "btn btn-sm pc-dup", "複製");
    dupBtn.type = "button";
    var delBtn = el("button", "btn btn-sm pc-del", "削除");
    delBtn.type = "button";
    opsTd.appendChild(copyBtn);
    opsTd.appendChild(dupBtn);
    opsTd.appendChild(delBtn);

    mainTr.appendChild(productCell.td);
    mainTr.appendChild(unitNetCell.td);
    mainTr.appendChild(quantityCell.td);
    mainTr.appendChild(taxCell.td);
    mainTr.appendChild(discountCell.td);
    mainTr.appendChild(taxCellOut);
    mainTr.appendChild(beforeGrossCell);
    mainTr.appendChild(afterGrossCell);
    mainTr.appendChild(afterNetCell);
    mainTr.appendChild(perUnitCell);
    mainTr.appendChild(opsTd);

    // 詳細表示（§5.3 推奨表示項目）: 行ごとの検算情報と警告
    var detailTd = el("td", "pc-detail-cell");
    detailTd.colSpan = 11;
    detailTr.appendChild(detailTd);
    detailTr.hidden = true;

    row.dom = {
      mainTr: mainTr,
      detailTr: detailTr,
      detailTd: detailTd,
      productInput: productInput,
      unitNetInput: unitNetInput,
      quantityInput: quantityInput,
      taxSelect: taxSelect,
      discountInput: discountInput,
      errs: {
        product: productCell.err,
        unitNet: unitNetCell.err,
        quantity: quantityCell.err,
        discount: discountCell.err
      },
      out: {
        afterTax: taxCellOut,
        beforeGross: beforeGrossCell,
        afterGross: afterGrossCell,
        afterNet: afterNetCell,
        perUnit: perUnitCell
      },
      copyBtn: copyBtn
    };

    // 入力イベント: 該当行のみ再計算し、保存を予約（FR-004 / FR-013）
    function onInput() {
      row.product = productInput.value;
      row.unitNet = unitNetInput.value;
      row.quantity = quantityInput.value;
      row.taxPct = Number(taxSelect.value);
      row.discount = discountInput.value;
      recalcRow(row);
      scheduleSave();
    }
    [productInput, unitNetInput, quantityInput, discountInput].forEach(function (inp) {
      inp.addEventListener("input", onInput);
    });
    taxSelect.addEventListener("change", onInput);

    // Excel等からの表形式貼り付け（複数セル）を各入力欄で受け付ける
    attachPaste(productInput, row, "product");
    attachPaste(unitNetInput, row, "unitNet");
    attachPaste(quantityInput, row, "quantity");
    attachPaste(discountInput, row, "discount");

    // フォーカスアウト時の整形（§5.4）
    unitNetInput.addEventListener("blur", function () {
      var v = PC.parseIntegerYen(unitNetInput.value);
      if (v != null && String(unitNetInput.value).trim() !== "") {
        unitNetInput.value = v.toLocaleString("ja-JP");
        onInput();
      }
    });
    quantityInput.addEventListener("blur", function () {
      var v = PC.parseIntegerYen(quantityInput.value);
      if (v != null && String(quantityInput.value).trim() !== "") {
        quantityInput.value = String(v);
        onInput();
      }
    });
    discountInput.addEventListener("blur", function () {
      var bp = PC.parseDiscountBp(discountInput.value);
      if (bp != null) {
        discountInput.value = PC.formatDiscountBp(bp);
        onInput();
      }
    });

    copyBtn.addEventListener("click", function () {
      if (row.status !== "ok" || !row.result || row.result.rmsOverflow) return;
      // §8.2: 画面表示値ではなく内部の整数計算結果をコピーする
      copyText(String(row.result.afterNet)).then(function () {
        flashButton(copyBtn, "コピー済");
      }).catch(function () {
        flashButton(copyBtn, "失敗");
      });
    });
    dupBtn.addEventListener("click", function () {
      addRow({
        product: row.product,
        unitNet: row.unitNet,
        quantity: row.quantity,
        taxPct: row.taxPct,
        discount: row.discount
      }, row);
      scheduleSave();
    });
    delBtn.addEventListener("click", function () {
      removeRow(row);
      scheduleSave();
    });

    return row;
  }

  /* ---------- 行の追加・削除 ---------- */

  function addRow(initial, afterRow) {
    var row = createRow(initial || {});
    if (afterRow) {
      var idx = rows.indexOf(afterRow);
      rows.splice(idx + 1, 0, row);
      afterRow.dom.detailTr.after(row.dom.mainTr, row.dom.detailTr);
    } else {
      rows.push(row);
      body.appendChild(row.dom.mainTr);
      body.appendChild(row.dom.detailTr);
    }
    recalcRow(row);
    return row;
  }

  function removeRow(row) {
    var idx = rows.indexOf(row);
    if (idx === -1) return;
    rows.splice(idx, 1);
    row.dom.mainTr.remove();
    row.dom.detailTr.remove();
    if (rows.length === 0) addRow({});
  }

  /* ---------- 再計算と表示更新 ---------- */

  function setOut(row, values) {
    var out = row.dom.out;
    if (!values) {
      ["afterTax", "beforeGross", "afterGross", "afterNet", "perUnit"].forEach(function (k) {
        out[k].textContent = "—";
        out[k].classList.add("pc-empty");
      });
      return;
    }
    out.afterTax.textContent = PC.formatYen(values.afterTax);
    out.beforeGross.textContent = PC.formatYen(values.beforeGross);
    out.afterGross.textContent = PC.formatYen(values.afterGross);
    out.afterNet.textContent = PC.formatYen(values.afterNet);
    out.perUnit.textContent = PC.formatPerUnit(values.perUnitGrossX100);
    ["afterTax", "beforeGross", "afterGross", "afterNet", "perUnit"].forEach(function (k) {
      out[k].classList.remove("pc-empty");
    });
  }

  function setErrors(row, errors) {
    ["product", "unitNet", "quantity", "discount"].forEach(function (field) {
      var errEl = row.dom.errs[field];
      var msg = errors[field];
      if (msg) {
        errEl.textContent = msg;
        errEl.hidden = false;
      } else {
        errEl.textContent = "";
        errEl.hidden = true;
      }
    });
  }

  function updateRowButtons(row) {
    if (!row) return;
    var copyable = row.status === "ok" && row.result && !row.result.rmsOverflow;
    row.dom.copyBtn.disabled = !copyable;
  }

  function recalcRow(row) {
    var check = PC.validateRow({
      product: row.product,
      unitNet: row.unitNet,
      quantity: row.quantity,
      taxPct: row.taxPct,
      discount: row.discount
    });
    row.status = check.status;
    setErrors(row, check.errors);

    if (check.status !== "ok") {
      // §8: エラー行・未使用行は結果を空欄にする。他行の計算は妨げない
      row.result = null;
      setOut(row, null);
      row.dom.detailTr.hidden = true;
      updateRowButtons(row);
      return;
    }

    var v = check.values;
    var r = PC.calculate(v.unitNet, v.quantity, v.taxPct, v.discountBp);
    row.result = r;
    setOut(row, r);

    // 詳細表示（§5.3）＋ 差額警告（FR-008）＋ 9桁超警告（§8）
    var detail = row.dom.detailTd;
    detail.textContent = "";
    var line = el("span", "pc-detail-line",
      "詳細：単品税額" + PC.formatYen(r.unitTax) +
      "／単品税込" + PC.formatYen(r.unitGross) +
      "／目標税込" + PC.formatYen(r.targetGross) +
      "／実効割引率" + PC.formatRateMil(r.effectiveRateMil) +
      "／目標差額" + PC.formatYen(r.targetDifference));
    detail.appendChild(line);

    if (r.rmsOverflow) {
      detail.appendChild(el("span", "pc-detail-warn pc-detail-error",
        PC.MESSAGES.rmsOverflow + "（" + PC.formatYen(r.afterNet) + "）"));
    } else if (r.targetDifference > 0) {
      detail.appendChild(el("span", "pc-detail-warn",
        "目標" + PC.formatYen(r.targetGross) + "は再現不可。目標を超えない" +
        PC.formatYen(r.afterGross) + "で算出しました。"));
    }

    var rowCopyBtn = el("button", "btn-link pc-rowcopy", "行コピー");
    rowCopyBtn.type = "button";
    rowCopyBtn.title = "入力値と計算結果をタブ区切りでコピー";
    rowCopyBtn.addEventListener("click", function () {
      copyText(rowTsv(row)).then(function () {
        rowCopyBtn.textContent = "コピー済";
        setTimeout(function () { rowCopyBtn.textContent = "行コピー"; }, 1200);
      }).catch(function () {
        rowCopyBtn.textContent = "失敗";
        setTimeout(function () { rowCopyBtn.textContent = "行コピー"; }, 1200);
      });
    });
    detail.appendChild(rowCopyBtn);

    row.dom.detailTr.hidden = false;
    updateRowButtons(row);
  }

  /* ---------- 行コピー・CSV出力（FR-009 / FR-011） ---------- */

  function rowValues(row) {
    var check = PC.validateRow({
      product: row.product,
      unitNet: row.unitNet,
      quantity: row.quantity,
      taxPct: row.taxPct,
      discount: row.discount
    });
    if (check.status !== "ok") return null;
    var v = check.values;
    var r = row.result;
    return [
      v.product,
      String(v.unitNet),
      String(v.quantity),
      v.taxPct + "%",
      PC.formatDiscountBp(v.discountBp) + "%",
      String(r.unitTax),
      String(r.beforeGross),
      String(r.targetGross),
      String(r.afterNet),
      String(r.afterTax),
      String(r.afterGross),
      (Math.floor(r.perUnitGrossX100 / 100)) + "." + String(r.perUnitGrossX100 % 100).padStart(2, "0"),
      PC.formatRateMil(r.effectiveRateMil),
      String(r.targetDifference)
    ];
  }

  function rowTsv(row) {
    var values = rowValues(row);
    return values ? values.join("\t") : "";
  }

  var CSV_HEADER = [
    "商品", "商品単価_税抜", "数量", "税率", "割引率", "単品税額", "割引前税込価格",
    "目標割引後税込価格", "割引後税抜価格", "税額_割引後", "割引後税込価格",
    "1個当たり_税込", "実効割引率", "目標差額"
  ];

  function csvEscape(value) {
    if (/[",\r\n]/.test(value)) {
      return '"' + value.replace(/"/g, '""') + '"';
    }
    return value;
  }

  function exportCsv() {
    var lines = [CSV_HEADER.join(",")];
    var count = 0;
    rows.forEach(function (row) {
      if (row.status !== "ok" || !row.result) return;
      var values = rowValues(row);
      if (!values) return;
      lines.push(values.map(csvEscape).join(","));
      count += 1;
    });
    if (count === 0) {
      csvNote.textContent = "出力できる行がありません。有効な入力の行を追加してください。";
      csvNote.hidden = false;
      setTimeout(function () { csvNote.hidden = true; }, 4000);
      return;
    }
    csvNote.hidden = true;
    // FR-011: UTF-8 BOM付き
    var blob = new Blob(["﻿" + lines.join("\r\n") + "\r\n"], { type: "text/csv;charset=utf-8" });
    var now = new Date();
    var pad = function (n) { return String(n).padStart(2, "0"); };
    var name = "価格計算_" + now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) +
      "-" + pad(now.getHours()) + pad(now.getMinutes()) + ".csv";
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }

  /* ---------- 全消去（FR-010） ---------- */

  function openModal() {
    modal.hidden = false;
    modalCancel.focus();
  }

  function closeModal() {
    modal.hidden = true;
  }

  function clearAll() {
    rows.slice().forEach(function (row) {
      row.dom.mainTr.remove();
      row.dom.detailTr.remove();
    });
    rows = [];
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (e) { /* noop */ }
    addRow({});
    closeModal();
  }

  /* ---------- 初期化 ---------- */

  addBtn.addEventListener("click", function () {
    var row = addRow({});
    row.dom.productInput.focus();
    scheduleSave();
  });
  csvBtn.addEventListener("click", exportCsv);
  clearBtn.addEventListener("click", openModal);
  modalOk.addEventListener("click", clearAll);
  modalCancel.addEventListener("click", closeModal);
  modal.addEventListener("click", function (ev) {
    if (ev.target === modal) closeModal();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !modal.hidden) closeModal();
  });

  var saved = loadState();
  if (saved) {
    saved.forEach(function (d) { addRow(d); });
  } else {
    addRow({});
  }
})();
