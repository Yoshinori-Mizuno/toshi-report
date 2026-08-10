"""
transactions.csv (購入履歴) と fetch_prices.py で取得する現在価格から、
証券会社・口座種別ごとの保有資産の評価額・評価損益を計算し、history.csv に
実行日ごとのスナップショットを積み上げながら index.html を生成する。

■ transactions.csv (自分で編集して増やしていくファイル)
  列: date, broker, account_type, code, quantity, unit_price, note
    date          購入日、または集約行の集計基準日 (YYYY-MM-DD)
    broker        証券会社名。例: SBI証券 / 楽天証券
    account_type  口座種別。例: 特定口座 / NISA(つみたて投資枠) / NISA(成長投資枠) / 旧NISA
    code          銘柄コード。fetch_prices.CODES のコードと一致させる
                  (例: 03311187, 2244.T)
    quantity      購入口数(投資信託)または株数(ETF/株式)
    unit_price    その行の取得金額(実際に支払った金額)。円建てで入力する。
                  外貨建て商品(米国株等)も、決済時のレートで円換算した
                  金額を入力する
    note          任意メモ(空でよい)。外貨建て取引は元の外貨金額などを
                  記録しておくとよい(例: 「スポット-SPCX(270ドル)」)

  同じ (broker, account_type, code) の組み合わせで複数行に分けて積立購入を
  記録すると、取得金額・合計保有口数を自動集計する(平均取得単価は
  取得金額合計 / 保有口数から逆算して表示する)。売却には対応していない
  (買い増しのみを前提)。

  ■ 詳細な約定履歴が無い過去分の扱い
  証券会社の約定履歴が例えば直近2年分しか遡れない場合、それより前の保有分は
  1行の「集約行」として計上する。証券会社の保有画面に表示されている
  「保有数量」「取得金額(合計)」をそのまま quantity / unit_price に入力し、
  date には集計時点の日付(例: 履歴を遡れる最も古い日の前日)、note には
  「2024年7月末までの累計(詳細履歴なし)」のように分かるメモを入れておく。
  例:
    2024-07-31,SBI証券,特定口座,03311187,50000,190000,2024年7月末までの累計(詳細履歴なし)
    2024-08-15,SBI証券,特定口座,03311187,10000,41000,積立
  以降は実際の約定履歴を通常の行として追記していけばよい。

■ history.csv (このスクリプトが実行のたびに自動更新する。手編集不要)
  列: date, total_cost, total_value, total_gain, total_gain_rate,
      sbi_cost, sbi_value, sbi_gain, sbi_gain_rate,
      rakuten_cost, rakuten_value, rakuten_gain, rakuten_gain_rate
  同じ日付に複数回実行した場合はその日の行を上書きする。
  BROKERS に無い証券会社は合算(total_*)には含まれるが、証券会社別の内訳
  列は持たない。

■ data.json (このスクリプトが実行のたびに自動更新する。手編集不要)
  最後に取得できた価格のキャッシュ。Yahoo!側の価格欄が "---" になっている等で
  当日値を取得できなかった銘柄は、このファイルの前回値をそのまま使って
  レポートを生成する(その銘柄には「前回値」と注記する)。他の銘柄は
  通常どおり当日値で更新される。
"""

import csv
import io
import os
import time
from datetime import date, datetime, timedelta

import fetch_prices as fp

TRANSACTIONS_FILE = "transactions.csv"
HISTORY_FILE = "history.csv"

# transactions.csv はExcel等での手編集を想定し、UTF-8以外にShift-JIS(CP932)で
# 保存される場合にも対応する。
TRANSACTIONS_ENCODINGS = ("utf-8-sig", "cp932")


def _read_text_any_encoding(path: str, encodings=TRANSACTIONS_ENCODINGS) -> str:
    with open(path, "rb") as f:
        data = f.read()
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(
        f"{path} の文字コードを判定できません(試行: {', '.join(encodings)})"
    )

# 証券会社別の内訳を history.csv / グラフに持たせる対象と、その列キー
BROKERS = [("SBI証券", "sbi"), ("楽天証券", "rakuten")]

ACCOUNT_TYPE_ORDER = ["特定口座", "NISA(つみたて投資枠)", "NISA(成長投資枠)", "旧NISA"]

HISTORY_FIELDS = ["date", "total_cost", "total_value", "total_gain", "total_gain_rate"]
for _name, _key in BROKERS:
    HISTORY_FIELDS += [f"{_key}_cost", f"{_key}_value", f"{_key}_gain", f"{_key}_gain_rate"]


def unit_basis(code: str) -> int:
    """投資信託は基準価額が10,000口あたりの表示のため10000、株式/ETF(コードに'.'を含む、
    またはSPCXのように数字を含まない米国株ティッカー)は1株単位。"""
    return 1 if "." in code or code.isalpha() else 10000


def account_sort_key(account_type: str):
    if account_type in ACCOUNT_TYPE_ORDER:
        return (0, ACCOUNT_TYPE_ORDER.index(account_type))
    return (1, account_type)


def broker_sort_key(broker: str):
    order = [name for name, _ in BROKERS]
    if broker in order:
        return (0, order.index(broker))
    return (1, broker)


def load_transactions(path: str) -> list:
    if not os.path.exists(path):
        return []
    text = _read_text_any_encoding(path)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    rows = []
    for i, row in enumerate(reader, start=2):
        if not row.get("code"):
            continue
        broker = (row.get("broker") or "").strip()
        account_type = (row.get("account_type") or "").strip()
        if not broker or not account_type:
            raise ValueError(
                f"transactions.csv の{i}行目: broker/account_type が未入力です"
            )
        rows.append(
            {
                "date": (row.get("date") or "").strip(),
                "broker": broker,
                "account_type": account_type,
                "code": row["code"].strip(),
                "quantity": float(row["quantity"]),
                "unit_price": float(row["unit_price"]),
                "note": (row.get("note") or "").strip(),
            }
        )
    return rows


def aggregate_by_key(transactions: list) -> dict:
    holdings = {}
    for t in transactions:
        key = (t["broker"], t["account_type"], t["code"])
        h = holdings.setdefault(key, {"quantity": 0.0, "cost_amount": 0.0})
        h["quantity"] += t["quantity"]
        h["cost_amount"] += t["unit_price"]
    return holdings


def build_detail_rows(holdings: dict, price_records: list) -> list:
    price_by_code = {r["code"]: r for r in price_records}
    rows = []
    for (broker, account_type, code), h in holdings.items():
        if h["quantity"] <= 0:
            continue
        basis = unit_basis(code)
        price_rec = price_by_code.get(code)
        avg_unit_price = h["cost_amount"] / (h["quantity"] / basis) if h["quantity"] else 0.0

        row = {
            "broker": broker,
            "account_type": account_type,
            "code": code,
            "label": price_rec["label"] if price_rec else code,
            "name": price_rec.get("name") if price_rec else None,
            "url": price_rec.get("url") if price_rec else fp.BASE_URL.format(code=code),
            "quantity": h["quantity"],
            "avg_unit_price": avg_unit_price,
            "cost_amount": h["cost_amount"],
            "current_price": None,
            "market_value": None,
            "gain": None,
            "gain_rate": None,
            "day_change": None,
            "day_change_rate": None,
            "price_date": None,
            "price_error": None,
            "price_stale": False,
        }

        if price_rec and not price_rec.get("error") and price_rec.get("price_value") is not None:
            current_price = price_rec.get("price_value_jpy", price_rec["price_value"])
            market_value = h["quantity"] / basis * current_price
            row["current_price"] = current_price
            row["market_value"] = market_value
            row["gain"] = market_value - h["cost_amount"]
            row["gain_rate"] = (row["gain"] / h["cost_amount"] * 100) if h["cost_amount"] else None
            row["price_date"] = price_rec.get("date")
            row["price_stale"] = bool(price_rec.get("stale"))

            # 前日比(評価額ベース) = 保有数 × 1口(株)あたりの前日比。
            # 外貨建ては当日のレートで円換算する(為替変動分は考慮しない)。
            change_per_unit = price_rec.get("change_value")
            if change_per_unit is not None:
                if price_rec.get("currency") == "USD" and price_rec.get("fx_rate"):
                    change_per_unit *= price_rec["fx_rate"]
                row["day_change"] = h["quantity"] / basis * change_per_unit
                row["day_change_rate"] = price_rec.get("change_rate_value")
        else:
            row["price_error"] = price_rec.get("error") if price_rec else "価格未取得"

        rows.append(row)

    rows.sort(
        key=lambda r: (
            broker_sort_key(r["broker"]),
            account_sort_key(r["account_type"]),
            -(r["market_value"] if r["market_value"] is not None else -1),
        )
    )
    return rows


def sum_day_change(rows: list):
    """複数行の前日比(円)を合算し、前日の評価額に対する変化率も返す。
    前日比が取れていない行は合算対象から除く。"""
    contributing = [
        r for r in rows
        if r.get("day_change") is not None and r.get("market_value") is not None
    ]
    if not contributing:
        return None, None
    total = sum(r["day_change"] for r in contributing)
    prev_value = sum(r["market_value"] - r["day_change"] for r in contributing)
    rate = (total / prev_value * 100) if prev_value else None
    return total, rate


def aggregate_by_code(detail_rows: list) -> list:
    groups = {}
    for r in detail_rows:
        g = groups.setdefault(
            r["code"],
            {
                "code": r["code"],
                "label": r["label"],
                "name": r["name"],
                "url": r["url"],
                "quantity": 0.0,
                "cost_amount": 0.0,
                "current_price": r["current_price"],
                "price_date": r["price_date"],
                "price_error": None,
                "price_stale": r["price_stale"],
                "has_price": True,
                "members": [],
            },
        )
        g["quantity"] += r["quantity"]
        g["cost_amount"] += r["cost_amount"]
        g["members"].append(r)
        if r["price_error"]:
            g["has_price"] = False
            g["price_error"] = r["price_error"]

    rows = []
    for code, g in groups.items():
        basis = unit_basis(code)
        avg_unit_price = g["cost_amount"] / (g["quantity"] / basis) if g["quantity"] else 0.0
        members = g.pop("members")
        row = dict(g, avg_unit_price=avg_unit_price)
        if g["has_price"] and g["current_price"] is not None:
            market_value = g["quantity"] / basis * g["current_price"]
            row["market_value"] = market_value
            row["gain"] = market_value - g["cost_amount"]
            row["gain_rate"] = (row["gain"] / g["cost_amount"] * 100) if g["cost_amount"] else None
            row["day_change"], row["day_change_rate"] = sum_day_change(members)
        else:
            row["market_value"] = None
            row["gain"] = None
            row["gain_rate"] = None
            row["day_change"] = None
            row["day_change_rate"] = None
        rows.append(row)

    rows.sort(key=lambda r: r["market_value"] if r["market_value"] is not None else -1, reverse=True)
    return rows


def compute_group_totals(rows: list) -> dict:
    total_cost = sum(r["cost_amount"] for r in rows)
    priced_rows = [r for r in rows if r["market_value"] is not None]
    total_value = sum(r["market_value"] for r in priced_rows)
    priced_cost = sum(r["cost_amount"] for r in priced_rows)
    total_gain = total_value - priced_cost
    total_gain_rate = (total_gain / priced_cost * 100) if priced_cost else None
    missing = [r["label"] for r in rows if r["market_value"] is None]
    day_change, day_change_rate = sum_day_change(rows)
    return {
        "total_cost": total_cost,
        "total_value": total_value,
        "total_gain": total_gain,
        "total_gain_rate": total_gain_rate,
        "day_change": day_change,
        "day_change_rate": day_change_rate,
        "missing_labels": missing,
    }


def update_history(path: str, today_str: str, totals_all: dict, totals_by_broker: dict) -> list:
    rows_by_date = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows_by_date[row["date"]] = row

    def g(t):
        return {
            "cost": f"{t['total_cost']:.2f}",
            "value": f"{t['total_value']:.2f}",
            "gain": f"{t['total_gain']:.2f}",
            "gain_rate": f"{t['total_gain_rate']:.4f}" if t["total_gain_rate"] is not None else "",
        }

    all_vals = g(totals_all)
    new_row = {
        "date": today_str,
        "total_cost": all_vals["cost"],
        "total_value": all_vals["value"],
        "total_gain": all_vals["gain"],
        "total_gain_rate": all_vals["gain_rate"],
    }
    for name, key in BROKERS:
        t = totals_by_broker.get(name)
        if t is None:
            t = {"total_cost": 0.0, "total_value": 0.0, "total_gain": 0.0, "total_gain_rate": None}
        vals = g(t)
        new_row[f"{key}_cost"] = vals["cost"]
        new_row[f"{key}_value"] = vals["value"]
        new_row[f"{key}_gain"] = vals["gain"]
        new_row[f"{key}_gain_rate"] = vals["gain_rate"]

    rows_by_date[today_str] = new_row

    ordered_dates = sorted(rows_by_date.keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for d in ordered_dates:
            # 過去に古いスキーマで書かれた行や欠損列があっても落ちないよう補完する
            row = {field: rows_by_date[d].get(field, "") for field in HISTORY_FIELDS}
            writer.writerow(row)

    return [{field: rows_by_date[d].get(field, "") for field in HISTORY_FIELDS} for d in ordered_dates]


def fmt_qty(q: float) -> str:
    if q == int(q):
        return f"{int(q):,}"
    return f"{q:,.4f}"


def fmt_money(v):
    if v is None:
        return "—"
    return f"{v:,.0f}"


def fmt_signed_money(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.0f}"


def fmt_signed_pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def trend_class_of(v) -> str:
    if v is None:
        return "flat"
    if v > 0:
        return "up"
    if v < 0:
        return "down"
    return "flat"


def badge(trend_class: str, text: str) -> str:
    return f'<span class="change-badge {trend_class}">{text}</span>'


def day_change_cell(value, rate) -> str:
    """前日比(円)と変化率(%)を1セル分のHTMLにまとめる。"""
    if value is None:
        return '<span class="cell-val">—</span>'
    html = badge(trend_class_of(value), fmt_signed_money(value) + "円")
    if rate is not None:
        html += f'<span class="sub">({fmt_signed_pct(rate)})</span>'
    return f'<span class="cell-val">{html}</span>'


def current_price_cell(row) -> str:
    """現在価格セル。前回値で補完した銘柄には注記を付ける。"""
    html = fmt_money(row["current_price"])
    if row.get("price_stale"):
        html += '<span class="sub">(前回値)</span>'
    return f'<span class="cell-val">{html}</span>'


def parse_float(s):
    if s in (None, ""):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def compute_gain_comparisons(history_rows: list, today: date, current_gain) -> list:
    """評価損益(全体)の 前日比 / 1年前比 / 年初来 を、history.csv に十分な
    データが蓄積されている場合のみ算出する。データ不足の項目は返さない。"""
    if current_gain is None:
        return []

    today_str = today.isoformat()
    prior = sorted(
        (
            {"date": r["date"], "gain": parse_float(r.get("total_gain"))}
            for r in history_rows
            if r["date"] != today_str and parse_float(r.get("total_gain")) is not None
        ),
        key=lambda r: r["date"],
    )
    if not prior:
        return []

    comparisons = []

    # 前日比: 直近の記録との比較(実行が毎日でない場合は直近の実行との比較になる)
    last = prior[-1]
    comparisons.append(
        {"label": "前日比", "delta": current_gain - last["gain"], "compare_date": last["date"]}
    )

    # 1年前比: 365日前の時点に最も近い記録(前後45日以内にある場合のみ採用)
    year_ago_target = today - timedelta(days=365)
    year_ago_candidates = [r for r in prior if r["date"] <= year_ago_target.isoformat()]
    if year_ago_candidates:
        best = year_ago_candidates[-1]
        best_date = datetime.strptime(best["date"], "%Y-%m-%d").date()
        if abs((year_ago_target - best_date).days) <= 45:
            comparisons.append(
                {"label": "1年前比", "delta": current_gain - best["gain"], "compare_date": best["date"]}
            )

    # 年初来: 今年1/1より前の最新の記録
    year_start = date(today.year, 1, 1)
    ytd_candidates = [r for r in prior if r["date"] < year_start.isoformat()]
    if ytd_candidates:
        best = ytd_candidates[-1]
        comparisons.append(
            {"label": "年初来", "delta": current_gain - best["gain"], "compare_date": best["date"]}
        )

    return comparisons


def render_gain_comparisons(comparisons: list) -> str:
    if not comparisons:
        return ""
    items = "".join(
        f'<div class="stat-sub"><span class="stat-sub-label">{c["label"]}</span>'
        f'{badge(trend_class_of(c["delta"]), fmt_signed_money(c["delta"]) + "円")}</div>'
        for c in comparisons
    )
    return f'<div class="stat-sub-list">{items}</div>'


def render_value_day_change(totals: dict) -> str:
    """評価額タイルに前日比(値動きによる増減)を添える。"""
    if totals.get("day_change") is None:
        return ""
    text = fmt_signed_money(totals["day_change"]) + "円"
    if totals.get("day_change_rate") is not None:
        text += f" ({fmt_signed_pct(totals['day_change_rate'])})"
    return (
        '<div class="stat-sub-list"><div class="stat-sub">'
        '<span class="stat-sub-label">前日比</span>'
        f'{badge(trend_class_of(totals["day_change"]), text)}'
        "</div></div>"
    )


def render_price_table(price_records: list) -> str:
    rows = []
    for r in price_records:
        if r.get("error"):
            rows.append(
                f"""
        <tr class="error-row">
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a></td>
          <td colspan="4" class="error" data-label="状態"><span class="cell-val">{r['error']}</span></td>
        </tr>"""
            )
            continue

        change_value = r.get("change_value")
        if change_value is None:
            trend_class, arrow = "flat", ""
        elif change_value > 0:
            trend_class, arrow = "up", "▲"
        elif change_value < 0:
            trend_class, arrow = "down", "▼"
        else:
            trend_class, arrow = "flat", "―"

        # 米国株はドル建てで取得するため、ドル表記＋円換算を併記する
        if r.get("currency") == "USD":
            price_value = r.get("price_value")
            price_cell = f"${price_value:,.2f}" if price_value is not None else "—"
            price_value_jpy = r.get("price_value_jpy")
            if price_value_jpy is not None:
                price_cell += f'<span class="sub">(≈{fmt_money(price_value_jpy)}円)</span>'
            change_disp = f"${change_value:+,.2f}" if change_value is not None else "—"
        else:
            price_cell = fmt_money(r.get("price_value"))
            change_disp = fmt_signed_money(change_value)

        # 当日値を取得できず前回値で補完した銘柄はその旨を明示する
        if r.get("stale"):
            price_cell += '<span class="sub">(前回値)</span>'

        rows.append(
            f"""
        <tr>
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name', '')}</span></td>
          <td class="num" data-label="基準価額/株価"><span class="cell-val">{price_cell}</span></td>
          <td class="num" data-label="前日比"><span class="cell-val">{badge(trend_class, f"{arrow} {change_disp}")}</span></td>
          <td class="num" data-label="前日比(%)"><span class="cell-val">{badge(trend_class, fmt_signed_pct(r.get('change_rate_value')))}</span></td>
          <td class="num sub" data-label="基準日"><span class="cell-val">{r.get('date') or '—'}</span></td>
        </tr>"""
        )
    return "".join(rows)


def render_code_table(code_rows: list, totals: dict) -> str:
    if not code_rows:
        return '<p class="empty">transactions.csv に購入履歴がまだありません。</p>'

    rows = []
    for r in code_rows:
        if r["price_error"]:
            rows.append(
                f"""
        <tr class="error-row">
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num" data-label="保有口数/株数"><span class="cell-val">{fmt_qty(r['quantity'])}</span></td>
          <td class="num" data-label="平均取得単価"><span class="cell-val">{fmt_money(r['avg_unit_price'])}</span></td>
          <td class="num error" data-label="現在価格"><span class="cell-val">{r['price_error']}</span></td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(r['cost_amount'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">—</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">—</span></td>
          <td class="num" data-label="前日比"><span class="cell-val">—</span></td>
        </tr>"""
            )
            continue

        gain = r["gain"]
        trend_class = trend_class_of(gain)
        rows.append(
            f"""
        <tr>
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num" data-label="保有口数/株数"><span class="cell-val">{fmt_qty(r['quantity'])}</span></td>
          <td class="num" data-label="平均取得単価"><span class="cell-val">{fmt_money(r['avg_unit_price'])}</span></td>
          <td class="num" data-label="現在価格">{current_price_cell(r)}</td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(r['cost_amount'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">{fmt_money(r['market_value'])}</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">{badge(trend_class, fmt_signed_money(gain) + '円')}<span class="sub">({fmt_signed_pct(r['gain_rate'])})</span></span></td>
          <td class="num" data-label="前日比">{day_change_cell(r['day_change'], r['day_change_rate'])}</td>
        </tr>"""
        )

    total_gain = totals["total_gain"]
    total_trend = trend_class_of(total_gain)
    total_row = f"""
        <tr class="total-row">
          <td class="label" data-label="銘柄">合計</td>
          <td class="num" data-label="保有口数/株数"><span class="cell-val">—</span></td>
          <td class="num" data-label="平均取得単価"><span class="cell-val">—</span></td>
          <td class="num" data-label="現在価格"><span class="cell-val">—</span></td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(totals['total_cost'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">{fmt_money(totals['total_value'])}</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">{badge(total_trend, fmt_signed_money(total_gain) + '円')}<span class="sub">({fmt_signed_pct(totals['total_gain_rate'])})</span></span></td>
          <td class="num" data-label="前日比">{day_change_cell(totals['day_change'], totals['day_change_rate'])}</td>
        </tr>"""

    missing_note = ""
    if totals["missing_labels"]:
        missing_note = (
            '<p class="note">価格未取得のため合計から除外: '
            + ", ".join(totals["missing_labels"])
            + "</p>"
        )

    return "".join(rows) + total_row + missing_note


def render_detail_table(detail_rows: list) -> str:
    if not detail_rows:
        return '<p class="empty">transactions.csv に購入履歴がまだありません。</p>'

    rows = []
    for r in detail_rows:
        if r["price_error"]:
            rows.append(
                f"""
        <tr class="error-row">
          <td class="sub" data-label="証券会社"><span class="cell-val">{r['broker']}</span></td>
          <td class="sub" data-label="口座種別"><span class="cell-val">{r['account_type']}</span></td>
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num" data-label="保有口数/株数"><span class="cell-val">{fmt_qty(r['quantity'])}</span></td>
          <td class="num" data-label="平均取得単価"><span class="cell-val">{fmt_money(r['avg_unit_price'])}</span></td>
          <td class="num error" data-label="現在価格"><span class="cell-val">{r['price_error']}</span></td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(r['cost_amount'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">—</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">—</span></td>
          <td class="num" data-label="前日比"><span class="cell-val">—</span></td>
        </tr>"""
            )
            continue

        gain = r["gain"]
        trend_class = trend_class_of(gain)
        rows.append(
            f"""
        <tr>
          <td class="sub" data-label="証券会社"><span class="cell-val">{r['broker']}</span></td>
          <td class="sub" data-label="口座種別"><span class="cell-val">{r['account_type']}</span></td>
          <td class="label" data-label="銘柄"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num" data-label="保有口数/株数"><span class="cell-val">{fmt_qty(r['quantity'])}</span></td>
          <td class="num" data-label="平均取得単価"><span class="cell-val">{fmt_money(r['avg_unit_price'])}</span></td>
          <td class="num" data-label="現在価格">{current_price_cell(r)}</td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(r['cost_amount'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">{fmt_money(r['market_value'])}</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">{badge(trend_class, fmt_signed_money(gain) + '円')}<span class="sub">({fmt_signed_pct(r['gain_rate'])})</span></span></td>
          <td class="num" data-label="前日比">{day_change_cell(r['day_change'], r['day_change_rate'])}</td>
        </tr>"""
        )
    return "".join(rows)


def render_broker_summary_table(totals_by_broker: dict) -> str:
    known = [name for name, _ in BROKERS]
    others = sorted(b for b in totals_by_broker if b not in known)
    order = known + others

    rows = []
    for broker in order:
        t = totals_by_broker.get(broker)
        if t is None:
            continue
        gain = t["total_gain"]
        trend_class = trend_class_of(gain)
        rows.append(
            f"""
        <tr>
          <td class="label" data-label="証券会社">{broker}</td>
          <td class="num" data-label="取得金額"><span class="cell-val">{fmt_money(t['total_cost'])}</span></td>
          <td class="num" data-label="評価額"><span class="cell-val">{fmt_money(t['total_value'])}</span></td>
          <td class="num" data-label="評価損益(率)"><span class="cell-val">{badge(trend_class, fmt_signed_money(gain) + '円')}<span class="sub">({fmt_signed_pct(t['total_gain_rate'])})</span></span></td>
          <td class="num" data-label="前日比">{day_change_cell(t['day_change'], t['day_change_rate'])}</td>
        </tr>"""
        )
    return "".join(rows)


def render_chart_svg(history_rows: list) -> str:
    usable_rows = [r for r in history_rows if r.get("total_value") not in (None, "")]
    if len(usable_rows) < 2:
        return '<p class="empty">資産推移グラフの表示には2件以上の履歴(=2回以上の実行)が必要です。</p>'

    width, height = 760, 300
    pad_l, pad_r, pad_t, pad_b = 64, 16, 20, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(usable_rows)
    dates = [r["date"] for r in usable_rows]

    def series_values(field):
        return [float(r[field]) if r.get(field) not in (None, "") else None for r in usable_rows]

    cost_values = series_values("total_cost")
    has_cost = any(v is not None for v in cost_values)

    line_defs = [{"key": "total_value", "label": "評価額(合算)", "css": "line-value", "dot_css": "pt-value"}]
    for name, key in BROKERS:
        line_defs.append(
            {"key": f"{key}_value", "label": f"評価額({name})", "css": f"line-{key}", "dot_css": f"pt-{key}"}
        )

    line_series = []
    for sd in line_defs:
        vals = series_values(sd["key"])
        if all(v is None for v in vals):
            continue
        line_series.append({**sd, "values": vals})

    all_vals = [v for v in cost_values if v is not None] + [
        v for s in line_series for v in s["values"] if v is not None
    ]
    if not all_vals:
        return '<p class="empty">資産推移グラフの表示には2件以上の履歴(=2回以上の実行)が必要です。</p>'

    y_min = min(0, min(all_vals))
    y_max = max(all_vals)
    y_max = y_max * 1.08 if y_max > 0 else 1.0
    y_range = (y_max - y_min) or 1.0

    def x_pos(i):
        return pad_l + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_pos(v):
        return pad_t + plot_h - (v - y_min) / y_range * plot_h

    gridlines = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h * k / 4:.1f}" '
        f'x2="{width - pad_r}" y2="{pad_t + plot_h * k / 4:.1f}" class="grid" />'
        for k in range(5)
    )
    y_labels = "".join(
        f'<text x="{pad_l - 8}" y="{pad_t + plot_h * k / 4 + 4:.1f}" '
        f'class="axis-label" text-anchor="end">'
        f"{fmt_money(y_max - (y_max - y_min) * k / 4)}</text>"
        for k in range(5)
    )

    bars = ""
    if has_cost:
        bar_w = max(min(plot_w / n * 0.5, 34), 4)
        y_zero = y_pos(0)
        for i, v in enumerate(cost_values):
            if v is None:
                continue
            y_top = y_pos(v)
            bar_h = max(y_zero - y_top, 0)
            bars += (
                # 端の棒がプロット領域からはみ出さないよう左右を丸め込む
                f'<rect x="{min(max(x_pos(i) - bar_w / 2, pad_l), width - pad_r - bar_w):.1f}" '
                f'y="{y_top:.1f}" width="{bar_w:.1f}" '
                f'height="{bar_h:.1f}" rx="4" class="bar-cost">'
                f"<title>{dates[i]}\n元本(合算): {v:,.0f}円</title></rect>"
            )

    def line_path(vals):
        # None(欠損)がある箇所で線を分割する
        segments, current = [], []
        for i, v in enumerate(vals):
            if v is None:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append(f"{x_pos(i):.1f},{y_pos(v):.1f}")
        if current:
            segments.append(current)
        return " ".join("M" + " L".join(seg) for seg in segments)

    paths = "".join(f'<path d="{line_path(s["values"])}" class="{s["css"]}" fill="none" />' for s in line_series)

    points = ""
    for s in line_series:
        for i, (d, v) in enumerate(zip(dates, s["values"])):
            if v is None:
                continue
            points += (
                f'<circle cx="{x_pos(i):.1f}" cy="{y_pos(v):.1f}" r="3.5" class="{s["dot_css"]}">'
                f"<title>{d}\n{s['label']}: {v:,.0f}円</title></circle>"
            )

    label_idx = sorted(set([0, n // 2, n - 1]))
    # 両端のラベルは中央揃えだと描画領域からはみ出すため、内側に寄せる
    def label_anchor(i):
        if i == 0:
            return "start"
        if i == n - 1:
            return "end"
        return "middle"

    x_labels = "".join(
        f'<text x="{x_pos(i):.1f}" y="{height - 6}" class="axis-label" '
        f'text-anchor="{label_anchor(i)}">{dates[i]}</text>'
        for i in label_idx
    )

    legend_items = ""
    if has_cost:
        legend_items += '<span class="legend-item"><span class="swatch swatch-bar bar-cost-swatch"></span>元本(合算)</span>'
    legend_items += "".join(
        f'<span class="legend-item"><span class="swatch {s["css"]}-swatch"></span>{s["label"]}</span>'
        for s in line_series
    )

    return f"""
    <div class="chart-legend">{legend_items}</div>
    <svg viewBox="0 0 {width} {height}" class="asset-chart" role="img" aria-label="資産推移グラフ">
      {gridlines}
      {y_labels}
      {bars}
      {paths}
      {points}
      {x_labels}
    </svg>
    """


def render_html(
    price_records,
    code_rows,
    detail_rows,
    totals_all,
    totals_by_broker,
    history_rows,
    generated_at,
    gain_comparisons,
) -> str:
    gain_trend = trend_class_of(totals_all["total_gain"])
    rate_trend = trend_class_of(totals_all["total_gain_rate"])
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>資産状況レポート</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f4f2ff;
    --page-accent: #fff6ec;
    --surface: #ffffff;
    --text-primary: #211c3d;
    --text-secondary: #5c5680;
    --text-muted: #928dae;
    --gridline: #eae5fb;
    --border: rgba(33, 28, 61, 0.09);
    --up: #0a9d5c;
    --up-bg: #e0faed;
    --down: #e0334f;
    --down-bg: #fdeaee;
    --accent-cost: #6d5ef8;
    --accent-value: #0ea5c4;
    --accent-gain: #ff8a3d;
    --accent-rate: #b350e0;
    --series-cost: #b7acfb;
    --series-value: #6d5ef8;
    --series-sbi: #ff8a3d;
    --series-rakuten: #0ea5c4;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 28px 14px 64px;
    background: linear-gradient(180deg, var(--page) 0%, var(--page-accent) 100%);
    background-attachment: fixed;
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{
    font-size: 1.5rem; margin: 0 0 4px; font-weight: 800;
    background: linear-gradient(90deg, var(--accent-cost), var(--accent-rate));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  h2 {{
    font-size: 1.05rem; margin: 40px 0 12px; font-weight: 700;
    padding-left: 12px; border-left: 5px solid var(--accent-value); color: var(--text-primary);
  }}
  .meta {{ color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 8px; }}
  .stat-row {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin: 16px 0 28px; }}
  @media (min-width: 600px) {{ .stat-row {{ grid-template-columns: repeat(4, 1fr); }} }}
  .stat-tile {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-top: 4px solid var(--tile-accent, var(--accent-value));
    border-radius: 14px;
    padding: 14px 16px;
    box-shadow: 0 2px 10px rgba(50, 30, 100, 0.06);
    min-width: 0;
  }}
  .stat-tile.tile-cost {{ --tile-accent: var(--accent-cost); }}
  .stat-tile.tile-value {{ --tile-accent: var(--accent-value); }}
  .stat-tile.tile-gain {{ --tile-accent: var(--accent-gain); }}
  .stat-tile.tile-rate {{ --tile-accent: var(--accent-rate); }}
  .stat-tile .stat-label {{ color: var(--text-muted); font-size: 0.74rem; font-weight: 600; }}
  .stat-tile .stat-value {{
    font-size: 1.35rem; font-weight: 800; margin-top: 4px; font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }}
  .stat-tile .stat-value.up {{ color: var(--up); }}
  .stat-tile .stat-value.down {{ color: var(--down); }}
  .stat-sub-list {{ margin-top: 10px; display: flex; flex-direction: column; gap: 5px; }}
  .stat-sub {{ display: flex; justify-content: space-between; align-items: center; font-size: 0.76rem; gap: 8px; }}
  .stat-sub-label {{ color: var(--text-muted); font-weight: 600; }}

  .change-badge {{
    display: inline-flex; align-items: center; gap: 3px;
    padding: 2px 9px; border-radius: 999px; font-weight: 700; font-size: 0.85em;
    white-space: nowrap;
  }}
  .change-badge.up {{ color: var(--up); background: var(--up-bg); }}
  .change-badge.down {{ color: var(--down); background: var(--down-bg); }}
  .change-badge.flat {{ color: var(--text-muted); background: var(--gridline); }}

  .table-scroll {{
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 2px 10px rgba(50, 30, 100, 0.05);
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; min-width: 640px; }}
  thead th {{
    text-align: right; font-weight: 700; color: var(--text-secondary);
    font-size: 0.76rem; padding: 12px 14px; border-bottom: 2px solid var(--gridline);
    white-space: nowrap; background: var(--page);
  }}
  thead th:first-child {{ text-align: left; border-top-left-radius: 14px; }}
  thead th:last-child {{ border-top-right-radius: 14px; }}
  tbody td {{
    padding: 11px 14px; border-bottom: 1px solid var(--gridline);
    text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: var(--page); }}
  td.label {{ text-align: left; white-space: normal; }}
  td.label a {{ color: var(--text-primary); font-weight: 700; text-decoration: none; }}
  td.label a:hover {{ text-decoration: underline; color: var(--accent-cost); }}
  td.sub {{ text-align: left; color: var(--text-secondary); font-size: 0.82rem; white-space: nowrap; }}
  .sub-name {{ display: block; color: var(--text-muted); font-size: 0.76rem; font-weight: 400; margin-top: 2px; }}
  td span.sub {{ display: inline-block; color: var(--text-muted); font-size: 0.76rem; font-weight: 400; }}
  tr.total-row td {{ font-weight: 800; border-top: 2px solid var(--accent-value); background: var(--page); }}
  td.error {{ text-align: left; color: var(--down); white-space: normal; }}
  .empty, .note {{ color: var(--text-muted); font-size: 0.85rem; }}
  .note {{ margin-top: 8px; }}

  .chart-legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; font-size: 0.8rem; color: var(--text-secondary); font-weight: 600; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 14px; height: 3px; border-radius: 2px; display: inline-block; }}
  .swatch-bar {{ height: 10px; border-radius: 3px; }}
  .bar-cost-swatch {{ background: var(--series-cost); }}
  .line-value-swatch {{ background: var(--series-value); }}
  .line-sbi-swatch {{ background: var(--series-sbi); }}
  .line-rakuten-swatch {{ background: var(--series-rakuten); }}
  .asset-chart {{ width: 100%; height: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; }}
  .bar-cost {{ fill: var(--series-cost); opacity: 0.75; }}
  .line-value {{ stroke: var(--series-value); stroke-width: 2.5; }}
  .line-sbi {{ stroke: var(--series-sbi); stroke-width: 2.5; }}
  .line-rakuten {{ stroke: var(--series-rakuten); stroke-width: 2.5; }}
  .grid {{ stroke: var(--gridline); stroke-width: 1; }}
  .pt-value {{ fill: var(--series-value); stroke: var(--surface); stroke-width: 1.5; }}
  .pt-sbi {{ fill: var(--series-sbi); stroke: var(--surface); stroke-width: 1.5; }}
  .pt-rakuten {{ fill: var(--series-rakuten); stroke: var(--surface); stroke-width: 1.5; }}
  .axis-label {{ fill: var(--text-muted); font-size: 10px; }}
  footer {{ margin-top: 28px; color: var(--text-muted); font-size: 0.78rem; }}

  @media (max-width: 640px) {{
    .table-scroll {{ overflow-x: visible; background: transparent; border: none; box-shadow: none; border-radius: 0; }}
    table {{ min-width: 0; }}
    thead {{ display: none; }}
    table, tbody, tr, td {{ display: block; width: 100%; }}
    tbody tr {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 10px 14px;
      margin-bottom: 10px;
      box-shadow: 0 2px 8px rgba(50, 30, 100, 0.05);
    }}
    tbody tr:last-child {{ margin-bottom: 0; }}
    tbody tr.total-row {{ border: 2px solid var(--accent-value); background: var(--surface); }}
    tbody tr.error-row {{ border-color: var(--down); }}
    td {{
      display: flex; justify-content: space-between; align-items: center; gap: 14px;
      padding: 7px 0; border-bottom: 1px dashed var(--gridline) !important;
      text-align: right; white-space: normal; background: transparent !important;
    }}
    td:last-child {{ border-bottom: none !important; }}
    td::before {{
      content: attr(data-label);
      color: var(--text-muted); font-weight: 600; font-size: 0.72rem;
      text-align: left; flex-shrink: 0;
    }}
    .cell-val {{ display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }}
    td.label {{ padding-top: 4px; padding-bottom: 4px; }}
    td.label::before {{ padding-top: 2px; }}
    td.label a {{ text-align: right; }}
    .stat-row {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>資産状況レポート</h1>
    <p class="meta">生成日時: {generated_at} / データ取得元: finance.yahoo.co.jp</p>

    <div class="stat-row">
      <div class="stat-tile tile-cost"><div class="stat-label">取得金額(元本・全体)</div><div class="stat-value">{fmt_money(totals_all['total_cost'])}円</div></div>
      <div class="stat-tile tile-value"><div class="stat-label">評価額(全体)</div><div class="stat-value">{fmt_money(totals_all['total_value'])}円</div>{render_value_day_change(totals_all)}</div>
      <div class="stat-tile tile-gain"><div class="stat-label">評価損益(全体)</div><div class="stat-value {gain_trend if gain_trend != 'flat' else ''}">{fmt_signed_money(totals_all['total_gain'])}円</div>{render_gain_comparisons(gain_comparisons)}</div>
      <div class="stat-tile tile-rate"><div class="stat-label">損益率(全体)</div><div class="stat-value {rate_trend if rate_trend != 'flat' else ''}">{fmt_signed_pct(totals_all['total_gain_rate'])}</div></div>
    </div>

    <h2>証券会社別サマリー</h2>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>証券会社</th>
            <th>取得金額</th>
            <th>評価額</th>
            <th>評価損益(率)</th>
            <th>前日比</th>
          </tr>
        </thead>
        <tbody>{render_broker_summary_table(totals_by_broker)}
        </tbody>
      </table>
    </div>

    <h2>銘柄別 保有資産(全口座合算)</h2>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>銘柄</th>
            <th>保有口数/株数</th>
            <th>平均取得単価</th>
            <th>現在価格</th>
            <th>取得金額</th>
            <th>評価額</th>
            <th>評価損益(率)</th>
            <th>前日比</th>
          </tr>
        </thead>
        <tbody>{render_code_table(code_rows, totals_all)}
        </tbody>
      </table>
    </div>
    <p class="note">投資信託の価格・単価は10,000口あたり、ETF/株式は1株あたりの金額です。</p>

    <h2>証券会社・口座種別ごとの保有明細</h2>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>証券会社</th>
            <th>口座種別</th>
            <th>銘柄</th>
            <th>保有口数/株数</th>
            <th>平均取得単価</th>
            <th>現在価格</th>
            <th>取得金額</th>
            <th>評価額</th>
            <th>評価損益(率)</th>
            <th>前日比</th>
          </tr>
        </thead>
        <tbody>{render_detail_table(detail_rows)}
        </tbody>
      </table>
    </div>

    <h2>資産推移</h2>
    {render_chart_svg(history_rows)}

    <h2>現在の基準価額・株価一覧</h2>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>銘柄</th>
            <th>基準価額/株価</th>
            <th>前日比</th>
            <th>前日比(%)</th>
            <th>基準日</th>
          </tr>
        </thead>
        <tbody>{render_price_table(price_records)}
        </tbody>
      </table>
    </div>

    <footer>本レポートはYahoo!ファイナンスの公開ページから自動取得した情報と transactions.csv の入力内容を基に算出しています。投資判断は自己責任で行ってください。</footer>
  </div>
</body>
</html>
"""


def main():
    today = date.today()

    print("価格取得中...")
    # data.json を上書きする前に前回値を読み込んでおく(取得失敗銘柄の補完用)
    price_cache = fp.load_price_cache()
    price_records = []
    for i, (label, code) in enumerate(fp.CODES):
        if i:
            # 間隔を空けずに連続取得すると全銘柄が5xxで弾かれるため
            time.sleep(fp.REQUEST_INTERVAL)
        record = fp.fetch_one(label, code, today)
        if record.get("error"):
            print(f"  -> {label}: エラー ({record['error']})")
        else:
            print(f"  -> {label}: {record.get('price')}")
        price_records.append(record)

    # 価格欄が "---" になっている等で当日値を取得できなかった銘柄は、
    # その銘柄だけ data.json の前回値をそのまま使う(他の銘柄は当日値で更新)。
    for r in fp.apply_cache_fallback(price_records, price_cache):
        print(
            f"  -> {r['label']}: 当日値を取得できなかったため前回値"
            f"({r.get('stale_date') or '日付不明'} 時点: {r.get('price')})を使用します"
        )

    # 全銘柄が失敗した場合、そのまま進むと評価額0円のレポートで
    # index.html と history.csv を上書きしてしまうため、ここで中断する。
    failed = [r for r in price_records if r.get("error")]
    if price_records and len(failed) == len(price_records):
        print("\n[中断] 全銘柄で価格を取得できませんでした。")
        print("       index.html / history.csv は更新していません。")
        print("       サイト側のアクセス制限(データセンターIPからの遮断など)の可能性があります。")
        print(f"       例: {failed[0]['label']}: {failed[0]['error']}")
        raise SystemExit(1)
    if failed:
        print(f"\n警告: {len(failed)}/{len(price_records)}銘柄の価格を取得できませんでした。")

    usd_records = [
        r for r in price_records
        if not r.get("error") and r.get("currency") == "USD" and r.get("price_value") is not None
    ]
    if usd_records:
        usdjpy_rate = fp.fetch_usdjpy_rate()
        if usdjpy_rate is not None:
            print(f"  -> USD/JPY: {usdjpy_rate:.3f}")
        for r in usd_records:
            # レート取得に失敗した場合は前回値として引き継いだレートで代用する
            rate = usdjpy_rate if usdjpy_rate is not None else r.get("fx_rate")
            if rate is None:
                r["error"] = "為替レート(USD/JPY)を取得できませんでした"
                continue
            r["fx_rate"] = rate
            r["price_value_jpy"] = r["price_value"] * rate

    transactions = load_transactions(TRANSACTIONS_FILE)
    holdings = aggregate_by_key(transactions)
    detail_rows = build_detail_rows(holdings, price_records)
    code_rows = aggregate_by_code(detail_rows)

    totals_all = compute_group_totals(detail_rows)
    brokers_present = sorted({r["broker"] for r in detail_rows})
    totals_by_broker = {
        broker: compute_group_totals([r for r in detail_rows if r["broker"] == broker])
        for broker in brokers_present
    }

    # 保有はあるのに評価額が0円になるのは価格取得が全滅した場合のみ。
    # 誤った0円スナップショットを history.csv に残さないようここでも中断する。
    if detail_rows and totals_all["total_value"] == 0:
        print("\n[中断] 保有銘柄の価格を1件も取得できず、評価額が0円になりました。")
        print("       index.html / history.csv は更新していません。")
        raise SystemExit(1)

    history_rows = update_history(HISTORY_FILE, today.isoformat(), totals_all, totals_by_broker)
    gain_comparisons = compute_gain_comparisons(history_rows, today, totals_all["total_gain"])

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(
        price_records,
        code_rows,
        detail_rows,
        totals_all,
        totals_by_broker,
        history_rows,
        generated_at,
        gain_comparisons,
    )
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    # 次回実行時に取得失敗した銘柄を補完できるよう、今回の価格を保存する
    fp.save_price_cache(price_records, generated_at)

    print(
        f"\n評価額: {fmt_money(totals_all['total_value'])}円 / "
        f"取得金額: {fmt_money(totals_all['total_cost'])}円 / "
        f"評価損益: {fmt_signed_money(totals_all['total_gain'])}円 ({fmt_signed_pct(totals_all['total_gain_rate'])})"
    )
    print("index.html / history.csv を更新しました。")


if __name__ == "__main__":
    main()
