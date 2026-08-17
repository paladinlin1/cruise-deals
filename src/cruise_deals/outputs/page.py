"""產生 GitHub Pages 用的靜態表格網頁。

刻意做成「完全自足」：CSS 與 JS 全部內嵌，不依賴任何 CDN，
因為 GitHub Pages 上沒有建置流程，而且外部資源掛掉會讓整頁失效。

表格由 Python 直接渲染（不是靠 JS 產生），JS 只負責排序／篩選／搜尋，
所以就算 JS 出錯，資料本身仍然看得到。
"""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path

from ..models import Deal
from .tabular import RunReport

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")

PAGE_TITLE = "郵輪 Last Minute Deals"

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --surface: #ffffff;
  --border: #dfe3e8;
  --text: #1b1f24;
  --muted: #61697a;
  --accent: #0b6bcb;
  --ok: #1a7f4b;
  --warn: #9a5b00;
  --warn-bg: #fff6e5;
  --stale: #8a6d00;
  --shadow: 0 1px 3px rgba(16, 24, 40, .08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c;
    --surface: #1c2027;
    --border: #2e343d;
    --text: #e6e9ee;
    --muted: #9aa4b2;
    --accent: #6cb2ff;
    --ok: #58d08b;
    --warn: #ffc46b;
    --warn-bg: #33280f;
    --stale: #ffc46b;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem 1rem 4rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC",
    "Microsoft JhengHei", Roboto, sans-serif;
  line-height: 1.5;
}
.wrap { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
.updated { color: var(--muted); font-size: .875rem; margin: 0 0 1rem; }
.stats { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1rem; }
.stat {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: .5rem; padding: .5rem .875rem; box-shadow: var(--shadow);
}
.stat b { display: block; font-size: 1.25rem; }
.stat span { color: var(--muted); font-size: .75rem; }
.sources { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
.src {
  font-size: .8125rem; padding: .3rem .625rem; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface);
}
.src.ok { color: var(--ok); }
.src.bad { color: var(--warn); background: var(--warn-bg); border-color: currentColor; }
.controls { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .875rem; }
.controls input, .controls select {
  padding: .45rem .6rem; border: 1px solid var(--border); border-radius: .375rem;
  background: var(--surface); color: inherit; font: inherit;
}
.controls input { flex: 1 1 14rem; }
.tablewrap {
  overflow-x: auto; background: var(--surface);
  border: 1px solid var(--border); border-radius: .5rem; box-shadow: var(--shadow);
}
.note {
  font-size: .8125rem; padding: .3rem .625rem; border-radius: .375rem;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--muted); margin-bottom: 1rem;
}
.note.warn { color: var(--warn); background: var(--warn-bg); border-color: currentColor; }
table { border-collapse: collapse; width: 100%; font-size: .875rem; }
th, td { padding: .625rem .75rem; text-align: left; white-space: nowrap; }
th {
  background: var(--surface); border-bottom: 2px solid var(--border);
  cursor: pointer; user-select: none; position: sticky; top: 0;
}
th:hover { color: var(--accent); }
th .arrow { opacity: .35; font-size: .7em; }
th.sorted .arrow { opacity: 1; color: var(--accent); }
tbody td { border-bottom: 1px solid var(--border); }
tbody tr:hover td { background: color-mix(in srgb, var(--accent) 7%, transparent); }
.route { white-space: normal; min-width: 13rem; }
/* 價格欄放得下台幣、原幣與比價徽章三樣東西，讓它自己換行，
   不然整張表會寬到必須橫向捲動才看得到「來源」與「連結」。 */
.price {
  font-variant-numeric: tabular-nums; font-weight: 600;
  white-space: normal; min-width: 7.5rem; max-width: 14rem;
}
.noprice { color: var(--muted); font-weight: 400; }
.orig { font-size: .75rem; font-weight: 400; color: var(--muted); margin-left: .35rem; }
.others { display: block; margin-top: .15rem; }
.badge {
  display: inline-block; font-size: .6875rem; padding: .1rem .4rem;
  border-radius: .25rem; border: 1px solid currentColor; color: var(--stale);
  margin-left: .375rem;
}
a { color: var(--accent); }
.empty { padding: 2.5rem 1rem; text-align: center; color: var(--muted); }
.hint { color: var(--muted); font-size: .8125rem; margin-top: .75rem; }
"""

_SCRIPT = """
(function () {
  var table = document.getElementById('deals');
  if (!table) return;
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var search = document.getElementById('q');
  var portSel = document.getElementById('port');
  var lineSel = document.getElementById('line');
  var empty = document.getElementById('empty');
  var count = document.getElementById('shown');

  function applyFilters() {
    var q = search.value.trim().toLowerCase();
    var port = portSel.value;
    var line = lineSel.value;
    var visible = 0;
    rows.forEach(function (row) {
      var show =
        (!port || row.dataset.port === port) &&
        (!line || row.dataset.line === line) &&
        (!q || row.dataset.search.indexOf(q) !== -1);
      row.hidden = !show;
      if (show) visible++;
    });
    count.textContent = visible;
    empty.hidden = visible !== 0;
  }

  function readSortValue(cell) {
    return cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent.trim();
  }

  function toNumber(value) {
    var n = parseFloat(value);
    return isNaN(n) ? Infinity : n;  // 無法解析者一律視為最大，排到最後
  }

  function sortBy(index, numeric, ascending) {
    var sorted = rows.slice().sort(function (a, b) {
      // 注意：不能寫成 `dataset.sort || textContent`。
      // 無報價時 data-sort 是空字串（falsy），會被誤退回讀取「洽詢報價」文字，
      // parseFloat 得到 NaN，導致「空值排最後」的判斷完全失效。
      var x = readSortValue(a.cells[index]);
      var y = readSortValue(b.cells[index]);
      if (numeric) {
        // 空值（洽詢報價）一律排最後，不論升冪降冪
        var nx = toNumber(x);
        var ny = toNumber(y);
        if (nx === ny) return 0;
        if (nx === Infinity) return 1;
        if (ny === Infinity) return -1;
        return ascending ? nx - ny : ny - nx;
      }
      return ascending ? x.localeCompare(y) : y.localeCompare(x);
    });
    var body = table.tBodies[0];
    sorted.forEach(function (row) { body.appendChild(row); });
    rows = sorted;
  }

  Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, index) {
    if (th.dataset.nosort !== undefined) return;
    th.addEventListener('click', function () {
      var ascending = th.dataset.dir !== 'asc';
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (other) {
        other.classList.remove('sorted');
        delete other.dataset.dir;
      });
      th.classList.add('sorted');
      th.dataset.dir = ascending ? 'asc' : 'desc';
      th.querySelector('.arrow').textContent = ascending ? '\\u25b2' : '\\u25bc';
      sortBy(index, th.dataset.numeric !== undefined, ascending);
    });
  });

  [search, portSel, lineSel].forEach(function (el) {
    el.addEventListener('input', applyFilters);
  });
  applyFilters();
})();
"""


def _format_price(price: Decimal | None) -> str:
  """千分位顯示；整數不補小數點。"""
  if price is None:
    return "洽詢報價"
  if price == price.to_integral_value():
    return f"{price:,.0f}"
  return f"{price:,.2f}"


# 各幣別的顯示前綴
_SYMBOLS = {"TWD": "NT$", "USD": "US$"}


def _money(price: Decimal | None, currency: str) -> str:
  """把金額寫成 "NT$89,876" / "US$2,812"；無報價回「洽詢報價」。"""
  if price is None:
    return "洽詢報價"
  return f"{_SYMBOLS.get(currency.upper(), currency + ' ')}{_format_price(price)}"


def _quote_text(quote: dict[str, str | None]) -> str:
  """other_sources 的一筆報價 -> 顯示字串（台幣為主）。"""
  raw = quote.get("price")
  if raw is None:
    return "洽詢報價"
  twd = quote.get("price_twd")
  currency = (quote.get("currency") or "").upper()
  if twd:
    return _money(Decimal(twd), "TWD")
  return _money(Decimal(raw), currency)


def _quote_title(source: str, quote: dict[str, str | None]) -> str:
  """比價徽章的 tooltip：把原幣寫出來，避免只看得到換算後的數字。"""
  raw = quote.get("price")
  if raw is None:
    return f"{source}：洽詢報價"
  currency = (quote.get("currency") or "").upper()
  return f"{source}：{_money(Decimal(raw), currency)}"


def _options(values: set[str], label: str) -> str:
  items = "".join(f'<option value="{escape(v)}">{escape(v)}</option>' for v in sorted(values))
  return f'<option value="">{escape(label)}</option>{items}'


def _price_cell(deal: Deal) -> str:
  """價格欄：台幣為主、原幣為輔。排序值一律用台幣，跨幣別才排得對。"""
  amount = deal.price_twd if deal.price_twd is not None else deal.price
  currency = "TWD" if deal.price_twd is not None else deal.currency
  main = _money(amount, currency)
  price_class = "price" if amount is not None else "price noprice"
  price_sort = str(amount) if amount is not None else ""

  # 原幣不是台幣時把原始報價一起顯示，讓人看得出換算來源
  original = ""
  if deal.price is not None and deal.price_twd is not None and deal.currency.upper() != "TWD":
    original = f'<span class="orig">{escape(_money(deal.price, deal.currency))}</span>'

  others = ""
  if deal.other_sources:
    detail = "、".join(
      f"{src} {_quote_text(quote)}" for src, quote in sorted(deal.other_sources.items())
    )
    title = "；".join(
      _quote_title(src, quote) for src, quote in sorted(deal.other_sources.items())
    )
    others = (
      f'<span class="others"><span class="badge" title="{escape(title)}">'
      f"{escape(detail)}</span></span>"
    )

  note = deal.price_note
  if deal.fx_rate is not None:
    note = f"{note}（1 USD = {deal.fx_rate} TWD）".strip()

  return (
    f'<td class="{price_class}" data-sort="{price_sort}" title="{escape(note)}">'
    f"{main}{original}{others}</td>"
  )


def _row(deal: Deal) -> str:
  """渲染一列航次。所有文字都經過跳脫。"""
  stale = ""
  if deal.stale_since:
    stale = f'<span class="badge" title="此來源近期擷取失敗，顯示的是舊資料">{deal.stale_since.isoformat()} 資料</span>'

  link = (
    f'<a href="{escape(deal.detail_url)}" target="_blank" rel="noopener">詳情</a>'
    if deal.detail_url
    else ""
  )

  # 台灣站的原始中文船名也要搜得到
  haystack = " ".join(
    [
      deal.ship_name,
      deal.ship_name_raw,
      deal.cruise_line,
      deal.depart_port,
      deal.arrive_port,
    ]
    + list(deal.ports_of_call)
  ).lower()

  ship = escape(deal.ship_name)
  if deal.ship_name_raw and deal.ship_name_raw != deal.ship_name:
    ship = f'<span title="{escape(deal.ship_name_raw)}">{ship}</span>'

  cells = [
    f'<td data-sort="{deal.sail_date.isoformat()}">{deal.sail_date.isoformat()}</td>',
    f"<td>{escape(deal.depart_port)}</td>",
    f'<td class="route">{escape(deal.arrive_port)}</td>',
    f"<td>{ship}</td>",
    f"<td>{escape(deal.cruise_line)}</td>",
    f'<td data-sort="{deal.nights}">{deal.nights}</td>',
    _price_cell(deal),
    f"<td>{escape(deal.source)}{stale}</td>",
    f"<td>{link}</td>",
  ]
  return (
    f'<tr data-port="{escape(deal.depart_port)}" data-line="{escape(deal.cruise_line)}"'
    f' data-search="{escape(haystack)}">' + "".join(cells) + "</tr>"
  )


def _source_badges(report: RunReport) -> str:
  badges = []
  for status in report.sources:
    if status.ok:
      badges.append(f'<span class="src ok">✓ {escape(status.source)}：{status.count} 筆</span>')
    else:
      badges.append(
        f'<span class="src bad">✕ {escape(status.source)}：擷取失敗 — '
        f"{escape(status.error or '未知原因')}（顯示為前次資料）</span>"
      )
  return "".join(badges)


def _stats(deals: list[Deal]) -> str:
  # 各來源幣別不同，最低價一律用台幣比
  prices = [d.price_twd for d in deals if d.price_twd is not None]
  cheapest = _money(min(prices), "TWD") if prices else "—"
  ports = len({d.depart_port for d in deals})
  return (
    f'<div class="stat"><b>{len(deals)}</b><span>總航次</span></div>'
    f'<div class="stat"><b>{escape(cheapest)}</b><span>最低價</span></div>'
    f'<div class="stat"><b>{ports}</b><span>出發港</span></div>'
  )


def _fx_note(report: RunReport) -> str:
  """匯率說明。沿用舊匯率時要看得出來，不要讓人以為是今天的匯率。"""
  rate = report.fx
  if rate is None:
    return (
      '<div class="note warn">沒有取得匯率，美元報價無法換算成台幣，'
      "跨來源比價與排序會不完整。</div>"
    )
  css = "note warn" if rate.stale else "note"
  suffix = "（本次未取得新匯率，沿用這一天的值）" if rate.stale else ""
  return (
    f'<div class="{css}">匯率 1 USD = {escape(str(rate.usd_twd))} TWD'
    f"・{escape(rate.as_of.isoformat())}・{escape(rate.source)}{suffix}</div>"
  )


def _warnings(report: RunReport) -> str:
  if not report.warnings:
    return ""
  items = "".join(f"<div>{escape(w)}</div>" for w in report.warnings)
  return f'<div class="note warn">{items}</div>'


def render(deals: list[Deal], report: RunReport) -> str:
  """產生完整的 HTML 字串。"""
  updated = report.generated_at.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")

  headers = [
    ("出發日期", False),
    ("出發港口", False),
    ("目的港口", False),
    ("郵輪名", False),
    ("船公司", False),
    ("航行天數", True),
    ("最低價格（台幣）", True),
    ("來源", False),
    ("連結", None),  # None 表示不可排序
  ]
  head_cells = []
  for label, numeric in headers:
    if numeric is None:
      head_cells.append(f"<th data-nosort>{label}</th>")
    else:
      attr = " data-numeric" if numeric else ""
      head_cells.append(f'<th{attr}>{label} <span class="arrow">↕</span></th>')

  body = "".join(_row(d) for d in deals)
  empty_hidden = "" if not deals else " hidden"
  empty_msg = "目前沒有符合條件的航次。" if not deals else "沒有符合篩選條件的航次。"

  ports = {d.depart_port for d in deals}
  lines = {d.cruise_line for d in deals if d.cruise_line}

  return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{PAGE_TITLE}</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>{PAGE_TITLE}</h1>
  <p class="updated">最後更新：{updated}（台北時間）・出發港：基隆／東京／橫濱・出發日期一個月內</p>

  <div class="stats">{_stats(deals)}</div>
  <div class="sources">{_source_badges(report)}</div>
  {_fx_note(report)}
  {_warnings(report)}

  <div class="controls">
    <input id="q" type="search" placeholder="搜尋郵輪、船公司、停靠港…" aria-label="搜尋">
    <select id="port" aria-label="出發港口">{_options(ports, "全部出發港")}</select>
    <select id="line" aria-label="船公司">{_options(lines, "全部船公司")}</select>
  </div>

  <div class="tablewrap">
    <table id="deals">
      <thead><tr>{"".join(head_cells)}</tr></thead>
      <tbody>{body}</tbody>
    </table>
    <div class="empty" id="empty"{empty_hidden}>{empty_msg}</div>
  </div>

  <p class="hint">顯示 <span id="shown">{len(deals)}</span> 筆・點欄位標題可排序・
  價格為每人最低艙房價（多為雙人房，實際條件以各平台頁面為準）・
  美元報價已依上方匯率換算成台幣，排序與比價皆以台幣為準。</p>
</div>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def write(path: Path, deals: list[Deal], report: RunReport) -> None:
  """把網頁寫到指定路徑。"""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(render(deals, report), encoding="utf-8")
