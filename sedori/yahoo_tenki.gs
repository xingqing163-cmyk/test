/**
 * ヤフショ月別シート → 「２０２６」シート 転記ツール（Google Apps Script）
 *
 * 使い方
 *   1. 「２０２６記帳」を開き、拡張機能 > Apps Script にこのファイルの内容を貼り付けて保存する
 *   2. スプレッドシートを再読み込みすると、メニューに「記帳ツール」が出る
 *   3. 初回だけ「記帳ツール > 初期設定」を実行する
 *      （月別シートのAE列・AF列に見出しを入れ、「転記設定」シートを作る。
 *        さらに、すでに手で転記してある行のAF列に「既存」と入れて対象から外す）
 *   4. 購入したら今までどおり月別シートに記入し、AE列に「今すぐ利用」のポイント数を入れる
 *   5. 「記帳ツール > このシートの未転記を２０２６へ転記」を押す
 *
 * 月別シート（ヤフショ（n））で読む列 … 7〜176行目
 *   N 購入品 / O 購入店 / P 日付 / S 購入金額 / T 付与予定ポイント合計
 *   AE 今すぐ利用（新設・手入力。空欄は0扱い）
 *   AF 転記No（新設・ツールが記入。転記したらそのNo、初期設定の時点で転記済みだった行は「既存」。
 *      何か入っている行は二重転記しない）
 *
 * 「２０２６」シートに書く列 … J列（商品名）が空いている最初の行から
 *   I 日付 / J 商品名 / K 店 / O 支払い方法 / P 仕入高 / Q 基本ぽ / U ヤフショ獲得すぐ利用
 *   H列のNoや X・AC などの数式は、もともと入っているものをそのまま使う
 */

const TENKI = {
  ledgerSheet: '２０２６',
  monthlyPrefix: 'ヤフショ（',
  settingsSheet: '転記設定',
  // 「未転記をまとめて転記」はこの日以降の購入だけを対象にする（これより前は手で転記済み）。
  // この日以降でも、初期設定の時点で「２０２６」に同じ日付・同じ金額の行がある分は「既存」になる。
  // 書式は必ず 'yyyy/MM/dd'（月・日は2桁）
  startDate: '2026/10/30',
  monthly: {
    headerRow: 5, firstRow: 7, lastRow: 176,
    name: 'N', store: 'O', date: 'P', amount: 'S', point: 'T', nowUse: 'AE', done: 'AF',
  },
  ledger: {
    firstRow: 4,
    no: 'H', date: 'I', name: 'J', store: 'K', pay: 'O', amount: 'P', point: 'Q', nowUse: 'U',
  },
  // 「転記設定」シートの初期値：月別シートのアカウント欄の開始行 → 「２０２６」O列の支払い方法
  // （2026年8〜10月の実績から作成。アカウントを入れ替えたら「転記設定」シートを直す）
  defaultPayLabels: [
    [7, 'pay メイン'], [18, 'pay 青12'], [26, 'pay ping'], [32, 'pay 黒15'],
    [38, 'pay motog4'], [44, 'pay unv'], [50, 'pay くつした'], [56, 'pay かなたろ'],
    [62, 'pay wishピンク'], [68, 'pay リベロ5G青'], [74, 'pay oppoスヌーピー'], [80, 'pay リベロ5G黒'],
    [86, 'pay oppoぷーさん'], [92, 'pay 白11'], [98, 'pay wish黒'], [104, 'pay かなみどり'],
    [110, 'pay かなもと'], [116, 'pay rxn'], [122, 'pay fmk'], [128, 'pay gcr'],
    [134, 'pay 茶スヌ'], [140, 'pay 青スヌ'], [146, 'pay あんぱんまん'], [153, 'pay れどみ'],
    [159, 'pay WISH白'], [165, 'pay 12mini黒'], [171, 'pay SE3白'],
  ],
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('記帳ツール')
    .addItem('このシートの未転記を２０２６へ転記', 'tenkiActiveSheet')
    .addItem('選択した行を２０２６へ転記', 'tenkiSelectedRows')
    .addSeparator()
    .addItem('初期設定（列見出し・転記設定シート）', 'tenkiSetup')
    .addToUi();
}

function tenkiSetup() {
  const ss = SpreadsheetApp.getActive();
  const m = TENKI.monthly;
  let headed = 0;
  ss.getSheets()
    .filter(s => s.getName().indexOf(TENKI.monthlyPrefix) === 0)
    .forEach(s => {
      const a = s.getRange(m.headerRow, col_(m.nowUse), 1, 1);
      const b = s.getRange(m.headerRow, col_(m.done), 1, 1);
      if (a.getValues()[0][0] === '') { a.setValues([['今すぐ利用']]); headed++; }
      if (b.getValues()[0][0] === '') b.setValues([['転記No']]);
    });
  let created = false;
  if (!ss.getSheetByName(TENKI.settingsSheet)) {
    const s = ss.insertSheet(TENKI.settingsSheet, ss.getNumSheets());
    const rows = [['開始行', '支払い方法']].concat(TENKI.defaultPayLabels);
    s.getRange(1, 1, rows.length, 2).setValues(rows);
    created = true;
  }
  const res = markExisting_(ss);
  SpreadsheetApp.getUi().alert(
    '初期設定が終わりました。\n\n' +
    '・見出しを入れた月別シート：' + headed + '枚（AE列「今すぐ利用」・AF列「転記No」）\n' +
    '・「転記設定」シート：' + (created ? '新しく作りました' : 'すでにあるのでそのままです') + '\n' +
    '・転記済みとしてAF列に「既存」と入れた行：' + res.marked + '行\n\n' +
    (res.pending.length
      ? 'まだ「' + TENKI.ledgerSheet + '」にない行（次の転記で対象になります）：\n' + res.pending.join('\n')
      : 'まだ「' + TENKI.ledgerSheet + '」にない行はありません。'));
}

/**
 * 初期設定の時点ですでにある行のAF列に「既存」と入れ、転記の対象から外す。
 * startDate より前の行はすべて、startDate 以降の行は「２０２６」に同じ日付・同じ金額の行があるものだけ。
 */
function markExisting_(ss) {
  const m = TENKI.monthly;
  const L = TENKI.ledger;
  const ledger = ss.getSheetByName(TENKI.ledgerSheet);
  const pool = {};
  const last = ledger.getLastRow();
  if (last >= L.firstRow) {
    const n = last - L.firstRow + 1;
    const dates = ledger.getRange(L.firstRow, col_(L.date), n, 1).getValues();
    const names = ledger.getRange(L.firstRow, col_(L.name), n, 1).getValues();
    const amounts = ledger.getRange(L.firstRow, col_(L.amount), n, 1).getValues();
    dates.forEach((d, i) => {
      if (!(d[0] instanceof Date) || String(names[i][0]).trim() === '') return;
      const day = dateKey_(d[0]);
      if (day < TENKI.startDate) return;
      const k = day + '|' + amounts[i][0];
      pool[k] = (pool[k] || 0) + 1;
    });
  }
  let marked = 0;
  const pending = [];
  ss.getSheets()
    .filter(s => s.getName().indexOf(TENKI.monthlyPrefix) === 0 && s.getName().indexOf('原紙') < 0)
    .forEach(s => {
      const rows = readMonthly_(s).filter(r => r.done === '' && r.date instanceof Date);
      if (!rows.length) return;
      const range = s.getRange(m.firstRow, col_(m.done), m.lastRow - m.firstRow + 1, 1);
      const done = range.getValues();
      rows.forEach(r => {
        const day = dateKey_(r.date);
        const k = day + '|' + r.amount;
        if (day < TENKI.startDate || pool[k] > 0) {
          if (day >= TENKI.startDate) pool[k]--;
          done[r.row - m.firstRow][0] = '既存';
          marked++;
        } else {
          pending.push(s.getName() + ' ' + r.row + '行目  ' + fmtDate_(r.date) + '  ' + short_(r.name));
        }
      });
      range.setValues(done);
    });
  return { marked: marked, pending: pending };
}

/** 開いている月別シートの、まだ転記していない行（startDate以降）をまとめて転記する */
function tenkiActiveSheet() {
  const sheet = monthlySheet_();
  if (!sheet) return;
  const rows = readMonthly_(sheet)
    .filter(r => !r.done && !(r.date instanceof Date && dateKey_(r.date) < TENKI.startDate));
  transfer_(sheet, rows, []);
}

/** 選択した行だけを転記する（startDateより前の行もこちらなら転記できる） */
function tenkiSelectedRows() {
  const sheet = monthlySheet_();
  if (!sheet) return;
  const list = sheet.getActiveRangeList();
  const picked = {};
  (list ? list.getRanges() : []).forEach(rg => {
    for (let r = rg.getRow(); r <= rg.getLastRow(); r++) picked[r] = true;
  });
  const rows = readMonthly_(sheet).filter(r => picked[r.row]);
  const skipped = rows.filter(r => r.done)
    .map(r => r.row + '行目（' + (r.done === '既存' ? '初期設定の時点で転記済み' : '転記済み No.' + r.done) + '）');
  transfer_(sheet, rows.filter(r => !r.done), skipped);
}

function transfer_(sheet, rows, skipped) {
  const ui = SpreadsheetApp.getUi();
  const ready = rows.filter(r => !r.problem);
  const notes = skipped.concat(rows.filter(r => r.problem).map(r => r.row + '行目（' + r.problem + '）'));
  const notesText = notes.length ? '\n\n転記しない行：\n' + notes.join('\n') : '';
  if (!ready.length) {
    ui.alert('転記する行はありません。' + notesText);
    return;
  }
  const labels = readPayLabels_();
  ready.forEach(r => { r.pay = payLabelFor_(labels, r.row); });
  const lines = ready.map(r =>
    r.row + '行目  ' + fmtDate_(r.date) + '  ' + short_(r.name) + '  ' + yen_(r.amount) + '円' +
    '  pt ' + yen_(r.point) + '  今すぐ利用 ' + yen_(r.nowUse) +
    '  ' + (r.pay || '※支払い方法が未設定') +
    (r.nowUse > (Number(r.point) || 0) ? '  ※今すぐ利用がポイント合計より大きい' : ''));
  const ok = ui.alert('「' + TENKI.ledgerSheet + '」シートへ転記',
    '次の' + ready.length + '件を転記します。\n\n' + lines.join('\n') + notesText,
    ui.ButtonSet.OK_CANCEL);
  if (ok !== ui.Button.OK) return;

  // 結果のダイアログはロックを外してから出す（開いている間ロックを持ち続けないように）
  ui.alert(write_(sheet, ready));
}

function write_(sheet, ready) {
  const lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    // ボタンの二度押しに備えて、ロックを取ってから転記済みかを読み直す
    const doneNow = sheet.getRange(TENKI.monthly.firstRow, col_(TENKI.monthly.done),
      TENKI.monthly.lastRow - TENKI.monthly.firstRow + 1, 1).getValues();
    const items = ready.filter(r => doneNow[r.row - TENKI.monthly.firstRow][0] === '');
    if (!items.length) return 'すでに転記済みでした。';
    const L = TENKI.ledger;
    const ledger = SpreadsheetApp.getActive().getSheetByName(TENKI.ledgerSheet);
    const top = nextLedgerRow_(ledger);
    const n = items.length;
    const lack = top + n - 1 - ledger.getMaxRows();
    if (lack > 0) ledger.insertRowsAfter(ledger.getMaxRows(), lack);
    const put = (letter, f) => ledger.getRange(top, col_(letter), n, 1).setValues(items.map(r => [f(r)]));
    put(L.date, r => r.date);
    put(L.name, r => r.name);
    put(L.store, r => r.store);
    put(L.pay, r => r.pay);
    put(L.amount, r => r.amount);
    put(L.point, r => r.point);
    put(L.nowUse, r => r.nowUse || '');
    const nos = ledger.getRange(top, col_(L.no), n, 1).getValues()
      .map((v, i) => v[0] !== '' ? v[0] : '行' + (top + i));
    items.forEach((r, i) => sheet.getRange(r.row, col_(TENKI.monthly.done), 1, 1).setValues([[nos[i]]]));
    SpreadsheetApp.flush();
    return n + '件を「' + TENKI.ledgerSheet + '」シートの' + top + '〜' + (top + n - 1) + '行目に転記しました。\n' +
      '（No.' + nos[0] + (n > 1 ? '〜' + nos[n - 1] : '') + '。月別シートのAF列に記録済み）\n\n' +
      '領収書リンク（L列）と、ポイントサイトを使った場合のS列は今までどおり入力してください。';
  } finally {
    lock.releaseLock();
  }
}

/** 月別シートの購入行を読む。商品名が空の行と、日付も金額もない行（メモなど）は返さない */
function readMonthly_(sheet) {
  const m = TENKI.monthly;
  const width = Math.max(col_(m.nowUse), col_(m.done));
  const values = sheet.getRange(m.firstRow, 1, m.lastRow - m.firstRow + 1, width).getValues();
  const out = [];
  values.forEach((v, i) => {
    const get = letter => v[col_(letter) - 1];
    if (String(get(m.name)).trim() === '') return;
    if (!(get(m.date) instanceof Date) && get(m.amount) === '') return;
    const r = {
      row: m.firstRow + i,
      name: get(m.name),
      store: get(m.store),
      date: get(m.date),
      amount: get(m.amount),
      point: typeof get(m.point) === 'number' ? get(m.point) : '',
      nowUse: get(m.nowUse) === '' ? 0 : get(m.nowUse),
      done: get(m.done),
    };
    if (!(r.date instanceof Date)) r.problem = '日付がない';
    else if (typeof r.amount !== 'number' || r.amount <= 0) r.problem = '購入金額がない';
    else if (String(r.store).trim() === '') r.problem = '購入店がない';
    else if (typeof r.nowUse !== 'number' || r.nowUse < 0) r.problem = '今すぐ利用が数字ではない';
    out.push(r);
  });
  return out;
}

/** 「２０２６」シートで、J列（商品名）が入っている最後の行の次の行 */
function nextLedgerRow_(ledger) {
  const L = TENKI.ledger;
  const last = ledger.getLastRow();
  if (last < L.firstRow) return L.firstRow;
  const names = ledger.getRange(L.firstRow, col_(L.name), last - L.firstRow + 1, 1).getValues();
  let i = names.length - 1;
  while (i >= 0 && String(names[i][0]).trim() === '') i--;
  return L.firstRow + i + 1;
}

function readPayLabels_() {
  const s = SpreadsheetApp.getActive().getSheetByName(TENKI.settingsSheet);
  const rows = s && s.getLastRow() >= 2
    ? s.getRange(2, 1, s.getLastRow() - 1, 2).getValues()
    : TENKI.defaultPayLabels;
  return rows
    .filter(r => typeof r[0] === 'number' && String(r[1]).trim() !== '')
    .sort((a, b) => a[0] - b[0]);
}

/** その行が属するアカウント欄（開始行がその行以下で最も近いもの）の支払い方法 */
function payLabelFor_(labels, row) {
  let label = '';
  labels.forEach(l => { if (l[0] <= row) label = String(l[1]).trim(); });
  return label;
}

function monthlySheet_() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const name = sheet.getName();
  if (name.indexOf(TENKI.monthlyPrefix) !== 0 || name.indexOf('原紙') >= 0) {
    SpreadsheetApp.getUi().alert('ヤフショ（月）のシートを開いてから実行してください。');
    return null;
  }
  // 初期設定（「既存」の記入）をする前に転記すると、手で転記済みの行まで二重に転記してしまう
  if (sheet.getRange(TENKI.monthly.headerRow, col_(TENKI.monthly.done), 1, 1).getValues()[0][0] !== '転記No') {
    SpreadsheetApp.getUi().alert('先に「記帳ツール > 初期設定」を実行してください。');
    return null;
  }
  return sheet;
}

function col_(letters) {
  return letters.split('').reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
}

/** シートのタイムゾーンでの日付文字列（startDate と比べるため 'yyyy/MM/dd'） */
function dateKey_(d) {
  return Utilities.formatDate(d, SpreadsheetApp.getActive().getSpreadsheetTimeZone(), 'yyyy/MM/dd');
}

function fmtDate_(d) {
  return Utilities.formatDate(d, SpreadsheetApp.getActive().getSpreadsheetTimeZone(), 'M/d');
}

function short_(name) {
  const s = String(name).split('\n')[0].trim();
  return s.length > 24 ? s.slice(0, 24) + '…' : s;
}

function yen_(v) {
  return typeof v === 'number' ? Math.round(v).toLocaleString('ja-JP') : '-';
}
