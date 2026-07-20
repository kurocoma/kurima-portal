/* =========================================================
   税込・割引価格計算（楽天RMS向け） — 計算ロジック
   要件定義書 v1.0 §4「計算ロジック」・付録A 疑似コード準拠。

   - RMSは税別価格で登録し、消費税額の1円未満は切り捨てる。
   - 割引後税抜価格は「RMS方式で再計算した税込価格が目標額を
     超えない最大の整数円」を採用する（§4.3）。
   - 金額はすべて BigInt の整数演算で処理し、浮動小数点を使わない
     （§4.6 数値計算の実装ルール / NFR-001）。
   - UIから分離した純粋関数とし、画面・CSV・テストで共用する
     （NFR-007 / §9.1）。browser では window.PriceCalc、
     node では module.exports で公開する。
   ========================================================= */
(function () {
  "use strict";

  var LIMITS = {
    unitNetMin: 1,
    unitNetMax: 999999999, // §5.1 IN-02
    quantityMin: 1,
    quantityMax: 9999, // §5.1 IN-03
    discountBpMin: 0,
    discountBpMax: 9999, // §5.1 IN-05: 0.00〜99.99%
    productMaxLen: 100, // §5.1 IN-01
    rmsPriceMax: 999999999 // §8: RMS登録価格が9桁を超えないこと
  };

  var TAX_RATES = [8, 10]; // §3.1

  // エラーメッセージ（§7.4 表示メッセージ / §8 入力チェック）
  var MESSAGES = {
    unitNetRequired: "商品単価（税抜）を入力してください。",
    unitNetInvalid: "商品単価（税抜）は1〜999,999,999円の整数で入力してください。",
    quantityRequired: "数量を入力してください。",
    quantityInvalid: "数量は1〜9,999の整数で入力してください。",
    discountInvalid: "割引率は0.00%以上99.99%以下で入力してください。",
    productTooLong: "商品は100文字以内で入力してください。",
    taxInvalid: "税率は8%または10%を選択してください。",
    rmsOverflow: "RMS登録価格が9桁を超えるため登録できません。"
  };

  function ceilDiv(a, b) {
    // §4.6: ceil_div(a, b) = floor((a + b − 1) ÷ b)
    return (a + b - 1n) / b;
  }

  /**
   * §4.2 基本式・付録A疑似コードの実装。
   * @param {number} unitNet   商品単価（税抜・整数円）
   * @param {number} quantity  数量（セットSKUに含める入数）
   * @param {number} taxPct    税率（8 または 10）
   * @param {number} discountBp 割引率（0.01%単位のベーシスポイント。5.00% → 500）
   * @returns 計算結果（金額は Number。最大でも約1.1e13 で安全に表現できる範囲）
   */
  function calculate(unitNet, quantity, taxPct, discountBp) {
    if (
      !Number.isInteger(unitNet) || unitNet < LIMITS.unitNetMin || unitNet > LIMITS.unitNetMax ||
      !Number.isInteger(quantity) || quantity < LIMITS.quantityMin || quantity > LIMITS.quantityMax ||
      TAX_RATES.indexOf(taxPct) === -1 ||
      !Number.isInteger(discountBp) || discountBp < LIMITS.discountBpMin || discountBp > LIMITS.discountBpMax
    ) {
      throw new RangeError("calculate: 入力値が許容範囲外です");
    }

    var P = BigInt(unitNet);
    var Q = BigInt(quantity);
    var T = BigInt(taxPct);
    var D = BigInt(discountBp);

    // ① 単品税額 = floor(P × R)（税額1円未満切り捨て）
    var unitTax = (P * T) / 100n;
    // ② 単品税込価格
    var unitGross = P + unitTax;
    // ③ 割引前税込価格（切り捨て後の単品税込価格 × 数量。付録B参照）
    var beforeGross = unitGross * Q;
    // ④ 目標割引後税込価格 = floor(割引前税込 × (10000 − bp) ÷ 10000)
    var targetGross = (beforeGross * (10000n - D)) / 10000n;

    // ⑤ 目標額を超えない最大の割引後税抜価格を逆算（§4.3）
    var gross = function (n) { return n + (n * T) / 100n; };
    var candidate = ceilDiv(targetGross * 100n, 100n + T);
    while (candidate > 0n && gross(candidate) > targetGross) candidate -= 1n;
    while (gross(candidate + 1n) <= targetGross) candidate += 1n;
    var afterNet = candidate;

    // ⑥⑦ 割引後税額・割引後税込価格を再計算
    var afterTax = (afterNet * T) / 100n;
    var afterGross = afterNet + afterTax;

    // ⑧ 1個当たり（税込）×100（小数第2位で四捨五入した整数。表示専用 §5.4）
    var perUnitGrossX100 = (afterGross * 200n + Q) / (2n * Q);
    // ⑨ 実効割引率（0.001%単位で四捨五入した整数。表示は小数第3位まで §5.4）
    var diff = beforeGross - afterGross;
    var effectiveRateMil = (diff * 200000n + beforeGross) / (2n * beforeGross);
    // 目標差額（§5.3。通常0円または1円）
    var targetDifference = targetGross - afterGross;

    return {
      unitTax: Number(unitTax),
      unitGross: Number(unitGross),
      beforeGross: Number(beforeGross),
      targetGross: Number(targetGross),
      afterNet: Number(afterNet),
      afterTax: Number(afterTax),
      afterGross: Number(afterGross),
      perUnitGrossX100: Number(perUnitGrossX100),
      effectiveRateMil: Number(effectiveRateMil),
      targetDifference: Number(targetDifference),
      // §8: RMS登録価格が9桁を超える場合は警告しコピーを抑止する
      rmsOverflow: Number(afterNet) > LIMITS.rmsPriceMax
    };
  }

  /* ---------- 入力パース（§5.4 カンマあり/なし双方を許容） ---------- */

  function normalizeNumericText(raw) {
    if (raw == null) return "";
    // 全角数字・全角記号を半角へ寄せ、カンマ・空白を除去する
    var s = String(raw).trim()
      .replace(/[０-９]/g, function (c) { return String.fromCharCode(c.charCodeAt(0) - 0xfee0); })
      .replace(/[．]/g, ".")
      .replace(/[，,\s]/g, "");
    return s;
  }

  /** 整数円のパース。小数・非数値は null（V02: 小数は整数入力エラー）。
   *  Excel貼り付けを考慮し「¥3,565」「3565円」の通貨表記も受け付ける。 */
  function parseIntegerYen(raw) {
    var s = normalizeNumericText(raw).replace(/^[¥￥]/, "").replace(/円$/, "");
    if (!/^\d+$/.test(s)) return null;
    return Number(s);
  }

  /** 割引率のパース。「5」「5.0」「5.00」→ 500bp。小数第3位以下は不可。
   *  Excel貼り付けを考慮し「5%」「5.00％」の%付き表記も受け付ける。 */
  function parseDiscountBp(raw) {
    var s = normalizeNumericText(raw).replace(/[%％]$/, "");
    var m = /^(\d{1,3})(?:\.(\d{1,2}))?$/.exec(s);
    if (!m) return null;
    var whole = Number(m[1]);
    var frac = m[2] == null ? 0 : Number((m[2] + "00").slice(0, 2));
    return whole * 100 + frac;
  }

  /** 税率のパース。「8」「8%」「10」「10%」（全角可）→ 8 / 10。それ以外は null。 */
  function parseTaxPct(raw) {
    var s = normalizeNumericText(raw).replace(/[%％]$/, "");
    var m = /^(8|10)(?:\.0+)?$/.exec(s);
    return m ? Number(m[1]) : null;
  }

  /** Excel等からの貼り付けテキストを行列（行×セル）に分解する。
   *  行は改行、セルはタブ区切り。末尾の空行（Excelが常に付ける改行）は除去する。 */
  function parseClipboardTable(text) {
    var s = String(text == null ? "" : text).replace(/[\r\n]+$/, "");
    if (s === "") return [];
    return s.split(/\r\n|\r|\n/).map(function (line) {
      return line.split("\t");
    });
  }

  /** 貼り付けテキストが複数セル（タブまたは複数行）かどうか。単一セルは通常貼り付けに任せる。 */
  function isTablePaste(text) {
    var s = String(text == null ? "" : text).replace(/[\r\n]+$/, "");
    return s.indexOf("\t") !== -1 || /[\r\n]/.test(s);
  }

  /**
   * 1行分の入力検証（§8）。
   * raw 値（画面の文字列）を受け取り、{ status, errors, values } を返す。
   * status: "empty"（未使用行 §8.1）/ "error" / "ok"
   */
  function validateRow(rawRow) {
    var product = rawRow.product == null ? "" : String(rawRow.product);
    var unitNetRaw = rawRow.unitNet == null ? "" : String(rawRow.unitNet).trim();
    var quantityRaw = rawRow.quantity == null ? "" : String(rawRow.quantity).trim();
    var discountRaw = rawRow.discount == null ? "" : String(rawRow.discount).trim();
    var taxPct = Number(rawRow.taxPct);

    // §8.1: 商品名を含め主要入力が空の行は未使用行としてエラーを出さない
    // （数量・税率・割引率は初期値を持つため、商品と単価の両方が空なら未使用と扱う）
    if (product === "" && unitNetRaw === "") {
      return { status: "empty", errors: {}, values: null };
    }

    var errors = {};

    if (product.length > LIMITS.productMaxLen) {
      errors.product = MESSAGES.productTooLong;
    }

    var unitNet = null;
    if (unitNetRaw === "") {
      errors.unitNet = MESSAGES.unitNetRequired;
    } else {
      unitNet = parseIntegerYen(unitNetRaw);
      if (unitNet == null || unitNet < LIMITS.unitNetMin || unitNet > LIMITS.unitNetMax) {
        errors.unitNet = MESSAGES.unitNetInvalid;
        unitNet = null;
      }
    }

    var quantity = null;
    if (quantityRaw === "") {
      errors.quantity = MESSAGES.quantityRequired;
    } else {
      quantity = parseIntegerYen(quantityRaw);
      if (quantity == null || quantity < LIMITS.quantityMin || quantity > LIMITS.quantityMax) {
        errors.quantity = MESSAGES.quantityInvalid;
        quantity = null;
      }
    }

    if (TAX_RATES.indexOf(taxPct) === -1) {
      errors.taxPct = MESSAGES.taxInvalid;
    }

    var discountBp = null;
    if (discountRaw === "") {
      // 割引率は必須（§5.1）だが初期値0%を持つため、空は0%として扱わずエラーにする
      errors.discount = MESSAGES.discountInvalid;
    } else {
      discountBp = parseDiscountBp(discountRaw);
      if (discountBp == null || discountBp < LIMITS.discountBpMin || discountBp > LIMITS.discountBpMax) {
        errors.discount = MESSAGES.discountInvalid;
        discountBp = null;
      }
    }

    if (Object.keys(errors).length > 0) {
      return { status: "error", errors: errors, values: null };
    }
    return {
      status: "ok",
      errors: {},
      values: { product: product, unitNet: unitNet, quantity: quantity, taxPct: taxPct, discountBp: discountBp }
    };
  }

  /* ---------- 表示フォーマット（§5.4） ---------- */

  function formatYen(n) {
    return Number(n).toLocaleString("ja-JP") + "円";
  }

  /** 1個当たり（×100整数）。整数円なら小数なし、そうでなければ小数第2位まで。例: 3,657.33円 */
  function formatPerUnit(x100) {
    var whole = Math.floor(x100 / 100);
    var frac = x100 % 100;
    var wholeText = whole.toLocaleString("ja-JP");
    if (frac === 0) return wholeText + "円";
    return wholeText + "." + String(frac).padStart(2, "0") + "円";
  }

  /** 実効割引率（0.001%単位整数）→ "5.004%" */
  function formatRateMil(mil) {
    var whole = Math.floor(mil / 1000);
    var frac = mil % 1000;
    return whole + "." + String(frac).padStart(3, "0") + "%";
  }

  /** 割引率bp → "5.00" （%記号なし） */
  function formatDiscountBp(bp) {
    var whole = Math.floor(bp / 100);
    var frac = bp % 100;
    return whole + "." + String(frac).padStart(2, "0");
  }

  var PriceCalc = {
    LIMITS: LIMITS,
    TAX_RATES: TAX_RATES,
    MESSAGES: MESSAGES,
    calculate: calculate,
    validateRow: validateRow,
    parseIntegerYen: parseIntegerYen,
    parseDiscountBp: parseDiscountBp,
    parseTaxPct: parseTaxPct,
    parseClipboardTable: parseClipboardTable,
    isTablePaste: isTablePaste,
    formatYen: formatYen,
    formatPerUnit: formatPerUnit,
    formatRateMil: formatRateMil,
    formatDiscountBp: formatDiscountBp
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = PriceCalc;
  } else {
    window.PriceCalc = PriceCalc;
  }
})();
