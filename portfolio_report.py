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
    unit_price    取得単価。投資信託は「10,000口あたりの基準価額」、
                  ETF/株式は「1株あたりの株価」で、現在価格と同じ基準で入力する
    note          任意メモ(空でよい)

  同じ (broker, account_type, code) の組み合わせで複数行に分けて積立購入を
  記録すると、平均取得単価・合計保有口数を自動集計する。売却には対応していない
  (買い増しのみを前提)。

  ■ 詳細な約定履歴が無い過去分の扱い
  証券会社の約定履歴が例えば直近2年分しか遡れない場合、それより前の保有分は
  1行の「集約行」として計上する。証券会社の保有画面に表示されている
  「保有数量」「平均取得単価」をそのまま quantity / unit_price に入力し、
  date には集計時点の日付(例: 履歴を遡れる最も古い日の前日)、note には
  「2024年7月末までの累計(詳細履歴なし)」のように分かるメモを入れておく。
  例:
    2024-07-31,SBI証券,特定口座,03311187,50000,38000,2024年7月末までの累計(詳細履歴なし)
    2024-08-15,SBI証券,特定口座,03311187,10000,41000,積立
  以降は実際の約定履歴を通常の行として追記していけばよい。

■ history.csv (このスクリプトが実行のたびに自動更新する。手編集不要)
  列: date, total_cost, total_value, total_gain, total_gain_rate,
      sbi_cost, sbi_value, sbi_gain, sbi_gain_rate,
      rakuten_cost, rakuten_value, rakuten_gain, rakuten_gain_rate
  同じ日付に複数回実行した場合はその日の行を上書きする。
  BROKERS に無い証券会社は合算(total_*)には含まれるが、証券会社別の内訳
  列は持たない。
"""

import csv
import os
from datetime import date, datetime

import fetch_prices as fp

TRANSACTIONS_FILE = "transactions.csv"
HISTORY_FILE = "history.csv"

# 証券会社別の内訳を history.csv / グラフに持たせる対象と、その列キー
BROKERS = [("SBI証券", "sbi"), ("楽天証券", "rakuten")]

ACCOUNT_TYPE_ORDER = ["特定口座", "NISA(つみたて投資枠)", "NISA(成長投資枠)", "旧NISA"]

HISTORY_FIELDS = ["date", "total_cost", "total_value", "total_gain", "total_gain_rate"]
for _name, _key in BROKERS:
    HISTORY_FIELDS += [f"{_key}_cost", f"{_key}_value", f"{_key}_gain", f"{_key}_gain_rate"]


def unit_basis(code: str) -> int:
    """投資信託は基準価額が10,000口あたりの表示のため10000、株式/ETF(コードに'.'を含む)は1株単位。"""
    return 1 if "." in code else 10000


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
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
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
        basis = unit_basis(t["code"])
        h["quantity"] += t["quantity"]
        h["cost_amount"] += t["quantity"] / basis * t["unit_price"]
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
            "price_date": None,
            "price_error": None,
        }

        if price_rec and not price_rec.get("error") and price_rec.get("price_value") is not None:
            current_price = price_rec["price_value"]
            market_value = h["quantity"] / basis * current_price
            row["current_price"] = current_price
            row["market_value"] = market_value
            row["gain"] = market_value - h["cost_amount"]
            row["gain_rate"] = (row["gain"] / h["cost_amount"] * 100) if h["cost_amount"] else None
            row["price_date"] = price_rec.get("date")
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
                "has_price": True,
            },
        )
        g["quantity"] += r["quantity"]
        g["cost_amount"] += r["cost_amount"]
        if r["price_error"]:
            g["has_price"] = False
            g["price_error"] = r["price_error"]

    rows = []
    for code, g in groups.items():
        basis = unit_basis(code)
        avg_unit_price = g["cost_amount"] / (g["quantity"] / basis) if g["quantity"] else 0.0
        row = dict(g, avg_unit_price=avg_unit_price)
        if g["has_price"] and g["current_price"] is not None:
            market_value = g["quantity"] / basis * g["current_price"]
            row["market_value"] = market_value
            row["gain"] = market_value - g["cost_amount"]
            row["gain_rate"] = (row["gain"] / g["cost_amount"] * 100) if g["cost_amount"] else None
        else:
            row["market_value"] = None
            row["gain"] = None
            row["gain_rate"] = None
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
    return {
        "total_cost": total_cost,
        "total_value": total_value,
        "total_gain": total_gain,
        "total_gain_rate": total_gain_rate,
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


def render_price_table(price_records: list) -> str:
    rows = []
    for r in price_records:
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
            trend_class, arrow = "flat", ""
        elif change_value > 0:
            trend_class, arrow = "up", "▲"
        elif change_value < 0:
            trend_class, arrow = "down", "▼"
        else:
            trend_class, arrow = "flat", "―"

        rows.append(
            f"""
        <tr>
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name', '')}</span></td>
          <td class="num">{fmt_money(r.get('price_value'))}</td>
          <td class="num change {trend_class}">{arrow} {fmt_signed_money(r.get('change_value'))}</td>
          <td class="num change {trend_class}">{fmt_signed_pct(r.get('change_rate_value'))}</td>
          <td class="num sub">{r.get('date') or '—'}</td>
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
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num">{fmt_qty(r['quantity'])}</td>
          <td class="num">{fmt_money(r['avg_unit_price'])}</td>
          <td class="num">{fmt_money(r['cost_amount'])}</td>
          <td colspan="3" class="error">{r['price_error']}</td>
        </tr>"""
            )
            continue

        gain = r["gain"]
        trend_class = "up" if gain > 0 else "down" if gain < 0 else "flat"
        rows.append(
            f"""
        <tr>
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num">{fmt_qty(r['quantity'])}</td>
          <td class="num">{fmt_money(r['avg_unit_price'])}</td>
          <td class="num">{fmt_money(r['cost_amount'])}</td>
          <td class="num">{fmt_money(r['current_price'])}</td>
          <td class="num">{fmt_money(r['market_value'])}</td>
          <td class="num change {trend_class}">{fmt_signed_money(gain)}<span class="sub">({fmt_signed_pct(r['gain_rate'])})</span></td>
        </tr>"""
        )

    total_gain = totals["total_gain"]
    total_trend = "up" if total_gain > 0 else "down" if total_gain < 0 else "flat"
    total_row = f"""
        <tr class="total-row">
          <td class="label">合計</td>
          <td class="num">—</td>
          <td class="num">—</td>
          <td class="num">{fmt_money(totals['total_cost'])}</td>
          <td class="num">—</td>
          <td class="num">{fmt_money(totals['total_value'])}</td>
          <td class="num change {total_trend}">{fmt_signed_money(total_gain)}<span class="sub">({fmt_signed_pct(totals['total_gain_rate'])})</span></td>
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
          <td class="sub">{r['broker']}</td>
          <td class="sub">{r['account_type']}</td>
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num">{fmt_qty(r['quantity'])}</td>
          <td class="num">{fmt_money(r['avg_unit_price'])}</td>
          <td class="num">{fmt_money(r['cost_amount'])}</td>
          <td colspan="3" class="error">{r['price_error']}</td>
        </tr>"""
            )
            continue

        gain = r["gain"]
        trend_class = "up" if gain > 0 else "down" if gain < 0 else "flat"
        rows.append(
            f"""
        <tr>
          <td class="sub">{r['broker']}</td>
          <td class="sub">{r['account_type']}</td>
          <td class="label"><a href="{r['url']}" target="_blank" rel="noopener">{r['label']}</a><span class="sub-name">{r.get('name') or ''}</span></td>
          <td class="num">{fmt_qty(r['quantity'])}</td>
          <td class="num">{fmt_money(r['avg_unit_price'])}</td>
          <td class="num">{fmt_money(r['cost_amount'])}</td>
          <td class="num">{fmt_money(r['current_price'])}</td>
          <td class="num">{fmt_money(r['market_value'])}</td>
          <td class="num change {trend_class}">{fmt_signed_money(gain)}<span class="sub">({fmt_signed_pct(r['gain_rate'])})</span></td>
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
        trend_class = "up" if gain > 0 else "down" if gain < 0 else "flat"
        rows.append(
            f"""
        <tr>
          <td class="label">{broker}</td>
          <td class="num">{fmt_money(t['total_cost'])}</td>
          <td class="num">{fmt_money(t['total_value'])}</td>
          <td class="num change {trend_class}">{fmt_signed_money(gain)}<span class="sub">({fmt_signed_pct(t['total_gain_rate'])})</span></td>
        </tr>"""
        )
    return "".join(rows)


def render_chart_svg(history_rows: list) -> str:
    usable_rows = [r for r in history_rows if r.get("total_value") not in (None, "")]
    if len(usable_rows) < 2:
        return '<p class="empty">資産推移グラフの表示には2件以上の履歴(=2回以上の実行)が必要です。</p>'

    width, height = 760, 280
    pad_l, pad_r, pad_t, pad_b = 64, 16, 16, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(usable_rows)
    dates = [r["date"] for r in usable_rows]

    def series_values(field):
        return [float(r[field]) if r.get(field) not in (None, "") else None for r in usable_rows]

    series_defs = [
        {"key": "total_cost", "label": "元本(合算)", "css": "line-cost", "dot_css": "pt-cost"},
        {"key": "total_value", "label": "評価額(合算)", "css": "line-s1", "dot_css": "pt-s1"},
    ]
    for name, key in BROKERS:
        series_defs.append(
            {"key": f"{key}_value", "label": f"評価額({name})", "css": f"line-{key}", "dot_css": f"pt-{key}"}
        )

    series = []
    for sd in series_defs:
        vals = series_values(sd["key"])
        if all(v is None for v in vals):
            continue
        series.append({**sd, "values": vals})

    all_vals = [v for s in series for v in s["values"] if v is not None]
    y_min = min(0, min(all_vals))
    y_max = max(all_vals)
    y_max = y_max * 1.08 if y_max > 0 else 1.0
    y_range = (y_max - y_min) or 1.0

    def x_pos(i):
        return pad_l + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_pos(v):
        return pad_t + plot_h - (v - y_min) / y_range * plot_h

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

    paths = "".join(f'<path d="{line_path(s["values"])}" class="{s["css"]}" fill="none" />' for s in series)

    points = ""
    for s in series:
        for i, (d, v) in enumerate(zip(dates, s["values"])):
            if v is None:
                continue
            points += (
                f'<circle cx="{x_pos(i):.1f}" cy="{y_pos(v):.1f}" r="3" class="{s["dot_css"]}">'
                f"<title>{d}\n{s['label']}: {v:,.0f}円</title></circle>"
            )

    label_idx = sorted(set([0, n // 2, n - 1]))
    x_labels = "".join(
        f'<text x="{x_pos(i):.1f}" y="{height - 6}" class="axis-label" text-anchor="middle">{dates[i]}</text>'
        for i in label_idx
    )

    legend = "".join(
        f'<span class="legend-item"><span class="swatch {s["css"]}-swatch"></span>{s["label"]}</span>'
        for s in series
    )

    return f"""
    <div class="chart-legend">{legend}</div>
    <svg viewBox="0 0 {width} {height}" class="asset-chart" role="img" aria-label="資産推移グラフ">
      {gridlines}
      {y_labels}
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
) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>資産状況レポート</title>
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
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
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
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
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
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 16px 64px;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  h2 {{ font-size: 1.05rem; margin: 40px 0 12px; }}
  .meta {{ color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 8px; }}
  .stat-row {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 28px; }}
  .stat-tile {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 18px;
    min-width: 150px;
    flex: 1;
  }}
  .stat-tile .stat-label {{ color: var(--text-muted); font-size: 0.75rem; }}
  .stat-tile .stat-value {{ font-size: 1.3rem; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }}
  .stat-tile .stat-value.up {{ color: var(--up); }}
  .stat-tile .stat-value.down {{ color: var(--down); }}
  .table-scroll {{
    overflow-x: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; min-width: 640px; }}
  thead th {{
    text-align: right; font-weight: 600; color: var(--text-muted);
    font-size: 0.76rem; padding: 12px 14px; border-bottom: 1px solid var(--gridline);
    white-space: nowrap;
  }}
  thead th:first-child {{ text-align: left; }}
  tbody td {{
    padding: 11px 14px; border-bottom: 1px solid var(--gridline);
    text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  td.label {{ text-align: left; white-space: normal; }}
  td.label a {{ color: var(--text-primary); font-weight: 600; text-decoration: none; }}
  td.label a:hover {{ text-decoration: underline; }}
  td.sub {{ text-align: left; color: var(--text-secondary); font-size: 0.82rem; white-space: nowrap; }}
  .sub-name {{ display: block; color: var(--text-muted); font-size: 0.76rem; font-weight: 400; margin-top: 2px; }}
  td span.sub {{ display: block; color: var(--text-muted); font-size: 0.76rem; font-weight: 400; text-align: right; }}
  td.change.up, .stat-value.up {{ color: var(--up); }}
  td.change.down, .stat-value.down {{ color: var(--down); }}
  td.change.flat {{ color: var(--text-muted); }}
  tr.total-row td {{ font-weight: 700; border-top: 2px solid var(--gridline); }}
  td.error {{ text-align: left; color: var(--down); }}
  .empty, .note {{ color: var(--text-muted); font-size: 0.85rem; }}
  .note {{ margin-top: 8px; }}
  .chart-legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; font-size: 0.8rem; color: var(--text-secondary); }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 14px; height: 3px; border-radius: 2px; display: inline-block; }}
  .line-cost-swatch {{ background: var(--text-muted); }}
  .line-s1-swatch {{ background: var(--series-1); }}
  .line-sbi-swatch {{ background: var(--series-2); }}
  .line-rakuten-swatch {{ background: var(--series-3); }}
  .asset-chart {{ width: 100%; height: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }}
  .line-cost {{ stroke: var(--text-muted); stroke-width: 2; stroke-dasharray: 5 4; }}
  .line-s1 {{ stroke: var(--series-1); stroke-width: 2; }}
  .line-sbi {{ stroke: var(--series-2); stroke-width: 2; }}
  .line-rakuten {{ stroke: var(--series-3); stroke-width: 2; }}
  .grid {{ stroke: var(--gridline); stroke-width: 1; }}
  .pt-cost {{ fill: var(--text-muted); }}
  .pt-s1 {{ fill: var(--series-1); }}
  .pt-sbi {{ fill: var(--series-2); }}
  .pt-rakuten {{ fill: var(--series-3); }}
  .axis-label {{ fill: var(--text-muted); font-size: 10px; }}
  footer {{ margin-top: 28px; color: var(--text-muted); font-size: 0.78rem; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>資産状況レポート</h1>
    <p class="meta">生成日時: {generated_at} / データ取得元: finance.yahoo.co.jp</p>

    <div class="stat-row">
      <div class="stat-tile"><div class="stat-label">取得金額(元本・全体)</div><div class="stat-value">{fmt_money(totals_all['total_cost'])}円</div></div>
      <div class="stat-tile"><div class="stat-label">評価額(全体)</div><div class="stat-value">{fmt_money(totals_all['total_value'])}円</div></div>
      <div class="stat-tile"><div class="stat-label">評価損益(全体)</div><div class="stat-value {'up' if totals_all['total_gain'] > 0 else 'down' if totals_all['total_gain'] < 0 else ''}">{fmt_signed_money(totals_all['total_gain'])}円</div></div>
      <div class="stat-tile"><div class="stat-label">損益率(全体)</div><div class="stat-value {'up' if (totals_all['total_gain_rate'] or 0) > 0 else 'down' if (totals_all['total_gain_rate'] or 0) < 0 else ''}">{fmt_signed_pct(totals_all['total_gain_rate'])}</div></div>
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
            <th>取得金額</th>
            <th>現在価格</th>
            <th>評価額</th>
            <th>評価損益(率)</th>
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
            <th>取得金額</th>
            <th>現在価格</th>
            <th>評価額</th>
            <th>評価損益(率)</th>
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
    price_records = []
    for label, code in fp.CODES:
        record = fp.fetch_one(label, code, today)
        if record.get("error"):
            print(f"  -> {label}: エラー ({record['error']})")
        else:
            print(f"  -> {label}: {record.get('price')}")
        price_records.append(record)

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

    history_rows = update_history(HISTORY_FILE, today.isoformat(), totals_all, totals_by_broker)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(
        price_records, code_rows, detail_rows, totals_all, totals_by_broker, history_rows, generated_at
    )
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(
        f"\n評価額: {fmt_money(totals_all['total_value'])}円 / "
        f"取得金額: {fmt_money(totals_all['total_cost'])}円 / "
        f"評価損益: {fmt_signed_money(totals_all['total_gain'])}円 ({fmt_signed_pct(totals_all['total_gain_rate'])})"
    )
    print("index.html / history.csv を更新しました。")


if __name__ == "__main__":
    main()
