"""
Yahoo!ファイナンス(finance.yahoo.co.jp)の投資信託・株式ページから
基準価額(株価)・前日比・日付を取得し、data.json と index.html を生成する。

対象ページは大きく3種類のHTML構造を持つ:
  1. 投資信託ページ  例: https://finance.yahoo.co.jp/quote/03311187
     -> <script>window.__PRELOADED_STATE__ = {...}</script> 内のJSONに
        "mainFundPriceBoard":{"fundPrices":{...}} として基準価額情報が入っている。
  2. 株式/ETFページ  例: https://finance.yahoo.co.jp/quote/2244.T
     -> PRELOADED_STATEが無く、価格はサーバーレンダリングされたHTML
        (class名に "CommonPriceBoard__price" 等を含む要素)に直接埋め込まれている。
  3. 米国株ページ  例: https://finance.yahoo.co.jp/quote/SPCX
     -> PRELOADED_STATE内のJSONに "mainUsStocksPriceBoard":{...} として
        ドル建ての価格情報が入っている。円換算にはfetch_usdjpy_rate()で
        取得するUSD/JPYレートを用いる(呼び出し側の責務)。

いずれのパターンも正規表現で解析し、共通のレコード形式にまとめる。
"""

import json
import re
import time
from datetime import date, timedelta, datetime

import requests

# ラベル, 銘柄コード
CODES = [
    ("一歩テック", "04314243"),
    ("Fang", "04311181"),
    ("SP500", "03311187"),
    ("日経225", "03311182"),
    ("NAS100", "29313233"),
    ("SOX", "29314233"),
    ("ゴルNAS(特)", "02311251"),
    ("iシェアーズゴールド(成)", "8931A236"),
    ("2244(成、ETF)", "2244.T"),
    ("楽天VTI", "9I312179"),
    ("オルカン", "0331418A"),
    ("SPCX", "SPCX"),
]

BASE_URL = "https://finance.yahoo.co.jp/quote/{code}"
FX_URL = "https://finance.yahoo.co.jp/quote/USDJPY=FX"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 10

# 取得結果のキャッシュ(前回値)。価格欄が "---" になっている等で当日の価格を
# 取得できなかった銘柄は、このファイルに残っている前回値で補完する。
DATA_FILE = "data.json"

# 前回値で補完する際に引き継ぐフィールド
CACHED_PRICE_FIELDS = (
    "date",
    "date_raw",
    "price",
    "price_value",
    "change",
    "change_value",
    "change_rate",
    "change_rate_value",
    "currency",
    "fx_rate",
)

# 短時間に連続アクセスすると Yahoo 側に弾かれ、全銘柄が 429/5xx になることがある。
# 銘柄間はこの秒数だけ間隔を空け、それでも弾かれた場合は指数バックオフで再試行する。
REQUEST_INTERVAL = 0.5
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

OUT_DIR = "."


def fetch_html(code: str) -> str:
    url = BASE_URL.format(code=code)
    last_error = None

    for attempt in range(MAX_RETRIES):
        if attempt:
            # 1回目の再試行は1秒、2回目は2秒…と待ち時間を伸ばす
            time.sleep(REQUEST_INTERVAL * (2 ** attempt))
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = e
            continue

        if resp.status_code in RETRYABLE_STATUS:
            last_error = requests.HTTPError(
                f"{resp.status_code} {resp.reason} (一時的なエラー。{MAX_RETRIES}回試行)",
                response=resp,
            )
            continue

        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    raise last_error


def extract_name(html: str, code: str) -> str:
    m = re.search(r"<title>(.*?)【", html)
    if m:
        return m.group(1).strip()
    return code


def parse_fund_page(html: str):
    """投資信託ページ (window.__PRELOADED_STATE__ 内の mainFundPriceBoard) を解析する。"""
    marker = '"mainFundPriceBoard"'
    idx = html.find(marker)
    if idx == -1:
        return None

    end = html.find('"currentTabNavigationKey"', idx)
    block = html[idx:end] if end != -1 else html[idx : idx + 1000]

    def field(key):
        m = re.search(rf'"{key}":"([^"]*)"', block)
        return m.group(1) if m else None

    price = field("price")
    if price is None:
        return None

    return {
        "date_raw": field("updateDate"),
        "price": price,
        "change": field("changePrice"),
        "change_rate": field("changePriceRate"),
    }


def parse_stock_page(html: str):
    """株式/ETFページ (サーバーレンダリングされたPriceBoard要素) を解析する。"""
    idx = html.find("CommonPriceBoard__priceInfo")
    if idx == -1:
        return None
    window_html = html[idx : idx + 4000]

    def next_value(pos):
        m = re.search(r'StyledNumber__value[^"]*">([^<]+)<', window_html[pos:])
        if not m:
            return None, pos
        return m.group(1), pos + m.end()

    price_anchor = window_html.find("CommonPriceBoard__price_")
    if price_anchor == -1:
        return None
    price, pos = next_value(price_anchor)
    if price is None:
        return None

    change_anchor = window_html.find("前日比", pos)
    change, pos = next_value(change_anchor if change_anchor != -1 else pos)
    change_rate, pos = next_value(pos)

    time_search_start = change_anchor if change_anchor != -1 else 0
    m = re.search(r"<time[^>]*>([^<]+)</time>", window_html[time_search_start:])
    date_raw = m.group(1) if m else None

    return {
        "date_raw": date_raw,
        "price": price,
        "change": change,
        "change_rate": change_rate,
    }


def parse_us_stock_page(html: str):
    """米国株ページ (window.__PRELOADED_STATE__ 内の mainUsStocksPriceBoard) を解析する。
    価格はドル建て。"""
    marker = '"mainUsStocksPriceBoard"'
    idx = html.find(marker)
    if idx == -1:
        return None

    end = html.find('"currentTabNavigationKey"', idx)
    block = html[idx:end] if end != -1 else html[idx : idx + 1500]

    def field(key):
        m = re.search(rf'"{key}":"([^"]*)"', block)
        return m.group(1) if m else None

    price = field("price")
    if price is None:
        return None

    return {
        "date_raw": field("japanUpdateTime"),
        "price": price,
        "change": field("priceChange"),
        "change_rate": field("priceChangeRate"),
        "currency": "USD",
    }


def fetch_usdjpy_rate():
    """USD/JPYの仲値(Bid/Askの平均)を取得する。取得・解析に失敗した場合はNoneを返す。"""
    try:
        resp = requests.get(FX_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except requests.RequestException:
        return None

    def extract(label):
        idx = html.find(label)
        if idx == -1:
            return None
        m = re.search(r'_FxPriceBoard__price_[^"]*">(.*?)</span></dd>', html[idx : idx + 500])
        if not m:
            return None
        text = re.sub(r"<[^>]+>", "", m.group(1))
        return to_float(text)

    bid = extract("Bid（売値）")
    ask = extract("Ask（買値）")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def to_iso_date(mmdd_raw: str, today: date) -> str:
    """'08/07' や '8/7' 形式の日付(年なし)を、直近の日付になるようISO形式に変換する。"""
    m = re.search(r"(\d{1,2})/(\d{1,2})", mmdd_raw or "")
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    candidate = date(today.year, month, day)
    # 12月のデータを1月に取得した場合など、未来日になったら前年扱いにする
    if candidate > today + timedelta(days=1):
        candidate = date(today.year - 1, month, day)
    return candidate.isoformat()


def to_float(value: str):
    if value is None:
        return None
    try:
        return float(value.replace(",", "").replace("+", ""))
    except ValueError:
        return None


def fetch_one(label: str, code: str, today: date) -> dict:
    record = {
        "label": label,
        "code": code,
        "url": BASE_URL.format(code=code),
        "error": None,
    }
    try:
        html = fetch_html(code)
    except requests.RequestException as e:
        record["error"] = f"取得失敗: {e}"
        return record

    record["name"] = extract_name(html, code)

    parsed = parse_fund_page(html) or parse_stock_page(html) or parse_us_stock_page(html)
    if parsed is None:
        record["error"] = "価格情報を解析できませんでした"
        return record

    record["date"] = to_iso_date(parsed["date_raw"], today)
    record["date_raw"] = parsed["date_raw"]
    record["price"] = parsed["price"]
    record["price_value"] = to_float(parsed["price"])
    record["change"] = parsed["change"]
    record["change_value"] = to_float(parsed["change"])
    record["change_rate"] = parsed["change_rate"]
    record["change_rate_value"] = to_float(parsed["change_rate"])
    record["currency"] = parsed.get("currency", "JPY")

    # 市場が開く前などYahoo!側の価格欄が "---" になっていることがある。
    # JSON自体は取得できているため従来はエラーと判定されず、price_value が
    # None のまま後続の計算に渡ってTypeErrorになっていた。ここで明示的に
    # エラー扱いにし、呼び出し側で前回値による補完(apply_cache_fallback)を行う。
    if record["price_value"] is None:
        record["error"] = f"価格が数値ではありません(表示値: {parsed['price']})"

    return record


def load_price_cache(path: str = None) -> dict:
    """前回出力した data.json を {銘柄コード: レコード} として読み込む。
    ファイルが無い/壊れている場合は空の辞書を返す。"""
    path = path or f"{OUT_DIR}/{DATA_FILE}"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}

    cache = {}
    for rec in data.get("records", []):
        if rec.get("code") and rec.get("price_value") is not None:
            cache[rec["code"]] = rec
    return cache


def apply_cache_fallback(records: list, cache: dict) -> list:
    """価格を取得できなかった銘柄を、data.json に残っている前回値で補完する。

    補完した銘柄は error を解除し、stale=True を立てて「前回値である」ことを
    レポート側で明示できるようにする。補完できた銘柄のリストを返す。"""
    recovered = []
    for record in records:
        if record.get("price_value") is not None:
            continue
        cached = cache.get(record.get("code"))
        if not cached:
            continue

        reason = record.get("error") or "価格を取得できませんでした"
        for key in CACHED_PRICE_FIELDS:
            if key in cached:
                record[key] = cached[key]
        if not record.get("name"):
            record["name"] = cached.get("name")

        record["error"] = None
        record["stale"] = True
        record["stale_reason"] = reason
        record["stale_date"] = cached.get("date")
        recovered.append(record)
    return recovered


def save_price_cache(records: list, generated_at: str, path: str = None) -> None:
    path = path or f"{OUT_DIR}/{DATA_FILE}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": generated_at, "records": records},
            f,
            ensure_ascii=False,
            indent=2,
        )


def render_html(records: list, generated_at: str) -> str:
    def fmt_signed(value, suffix=""):
        if value is None:
            return "—"
        sign = "+" if value > 0 else ""
        return f"{sign}{value:g}{suffix}"

    rows = []
    for r in records:
        if r.get("error"):
            rows.append(
                f"""
        <tr class="error-row">
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a></td>
          <td colspan="4" class="error">{r['error']}</td>
        </tr>"""
            )
            continue

        change_value = r.get("change_value")
        if change_value is None:
            trend_class = "flat"
            arrow = ""
        elif change_value > 0:
            trend_class = "up"
            arrow = "▲"
        elif change_value < 0:
            trend_class = "down"
            arrow = "▼"
        else:
            trend_class = "flat"
            arrow = "―"

        change_str = fmt_signed(r.get("change_value"))
        rate_str = fmt_signed(r.get("change_rate_value"), "%")

        rows.append(
            f"""
        <tr>
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="fund-name">{r.get('name', '')}</span></td>
          <td class="price">{r.get('price', '—')}</td>
          <td class="change {trend_class}">{arrow} {change_str}</td>
          <td class="change {trend_class}">{rate_str}</td>
          <td class="date">{r.get('date') or '—'}</td>
        </tr>"""
        )

    rows_html = "".join(rows)

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>投資信託・株式 基準価額レポート</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f9f9f7;
    --surface: #fcfcfb;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --up: #006300;
    --down: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --page: #0d0d0d;
      --surface: #1a1a19;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
      --up: #0ca30c;
      --down: #e66767;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --page: #0d0d0d;
    --surface: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --gridline: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --up: #0ca30c;
    --down: #e66767;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 16px 64px;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{
    max-width: 900px;
    margin: 0 auto;
  }}
  h1 {{
    font-size: 1.3rem;
    margin: 0 0 4px;
  }}
  .meta {{
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin: 0 0 20px;
  }}
  .table-scroll {{
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.92rem;
    min-width: 640px;
  }}
  thead th {{
    text-align: right;
    font-weight: 600;
    color: var(--text-muted);
    font-size: 0.78rem;
    padding: 12px 16px;
    border-bottom: 1px solid var(--gridline);
    white-space: nowrap;
  }}
  thead th:first-child {{ text-align: left; }}
  tbody td {{
    padding: 12px 16px;
    border-bottom: 1px solid var(--gridline);
    text-align: right;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  td.label {{
    text-align: left;
    white-space: normal;
  }}
  td.label a {{
    color: var(--text-primary);
    font-weight: 600;
    text-decoration: none;
  }}
  td.label a:hover {{ text-decoration: underline; }}
  .fund-name {{
    display: block;
    color: var(--text-muted);
    font-size: 0.78rem;
    font-weight: 400;
    margin-top: 2px;
  }}
  td.price {{ font-weight: 600; }}
  td.change.up {{ color: var(--up); }}
  td.change.down {{ color: var(--down); }}
  td.change.flat {{ color: var(--text-muted); }}
  td.date {{ color: var(--text-secondary); }}
  td.error {{
    text-align: left;
    color: var(--down);
  }}
  tr.error-row {{ background: transparent; }}
  footer {{
    margin-top: 20px;
    color: var(--text-muted);
    font-size: 0.78rem;
  }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>投資信託・株式 基準価額レポート</h1>
    <p class="meta">生成日時: {generated_at} / データ取得元: finance.yahoo.co.jp</p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>銘柄</th>
            <th>基準価額 / 株価</th>
            <th>前日比</th>
            <th>前日比(%)</th>
            <th>基準日</th>
          </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
      </table>
    </div>
    <footer>本レポートはYahoo!ファイナンスの公開ページから自動取得した情報です。投資判断は自己責任で行ってください。</footer>
  </div>
</body>
</html>
"""


def main():
    today = date.today()
    # data.json を上書きする前に前回値を読み込んでおく
    cache = load_price_cache()
    records = []
    for label, code in CODES:
        print(f"取得中: {label} ({code}) ...")
        record = fetch_one(label, code, today)
        if record.get("error"):
            print(f"  -> エラー: {record['error']}")
        else:
            print(
                f"  -> {record.get('name')}: {record.get('price')} "
                f"({record.get('change')}, {record.get('change_rate')}%) "
                f"基準日 {record.get('date')}"
            )
        records.append(record)
        time.sleep(REQUEST_INTERVAL)  # サーバーへの連続アクセスを避ける

    for r in apply_cache_fallback(records, cache):
        print(f"  -> {r['label']}: 前回値({r.get('stale_date') or '日付不明'})で補完しました")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    save_price_cache(records, generated_at)

    html = render_html(records, generated_at)
    with open(f"{OUT_DIR}/index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("\n完了: data.json, index.html を出力しました。")


if __name__ == "__main__":
    main()
