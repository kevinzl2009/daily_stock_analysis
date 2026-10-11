# -*- coding: utf-8 -*-
"""
每周 A股四大指数周记（微信公众号格式）

流程：取四大指数日线 -> 计算本周涨跌、均线位置 -> Gemini 写导语和观察（失败则用规则文字）
     -> 生成公众号 HTML 正文 + 封面图 -> 发送到邮箱（正文可直接复制粘贴到公众号编辑器）

只做市场观察，不输出任何买卖建议。
"""
import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
]
OUT = Path("reports/wechat")
OUT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 数据
def fetch_tencent(code, n=60):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    r = requests.get(url, params={"param": f"{code},day,,,{n},qfq"}, timeout=15)
    r.raise_for_status()
    d = r.json()["data"][code]
    rows = d.get("qfqday") or d.get("day") or []
    # [date, open, close, high, low, volume, ...]
    return [{"date": x[0], "open": float(x[1]), "close": float(x[2]),
             "high": float(x[3]), "low": float(x[4])} for x in rows]


def fetch_akshare(code):
    import akshare as ak
    df = ak.stock_zh_index_daily(symbol=code).tail(60)
    return [{"date": str(r.date)[:10], "open": float(r.open), "close": float(r.close),
             "high": float(r.high), "low": float(r.low)} for r in df.itertuples()]


def fetch(code):
    for fn in (fetch_tencent, fetch_akshare):
        try:
            rows = fn(code)
            if len(rows) >= 25:
                return rows
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {fn.__name__} {code}: {e}")
    raise RuntimeError(f"无法获取 {code} 行情")


def week_key(date_str):
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return (y, w)


def summarize(code, name):
    rows = fetch(code)
    last_week = week_key(rows[-1]["date"])
    this_week = [r for r in rows if week_key(r["date"]) == last_week]
    before = [r for r in rows if week_key(r["date"]) < last_week]
    prev_close = before[-1]["close"]
    close = rows[-1]["close"]
    closes = [r["close"] for r in rows]
    ma20 = sum(closes[-20:]) / 20
    ma5 = sum(closes[-5:]) / 5
    return {
        "name": name,
        "code": code,
        "close": close,
        "week_chg": (close / prev_close - 1) * 100,
        "week_high": max(r["high"] for r in this_week),
        "week_low": min(r["low"] for r in this_week),
        "days": len(this_week),
        "ma5": ma5,
        "ma20": ma20,
        "above_ma20": close >= ma20,
        "first_date": this_week[0]["date"],
        "last_date": rows[-1]["date"],
    }


# ---------------------------------------------------------------- 文字
def fmt_chg(x):
    return f"{x:+.2f}%"


def rule_text(data):
    up = [d for d in data if d["week_chg"] > 0]
    best = max(data, key=lambda d: d["week_chg"])
    worst = min(data, key=lambda d: d["week_chg"])
    above = [d["name"] for d in data if d["above_ma20"]]
    if len(up) == 4:
        mood, short = "四大指数本周全线收涨", "四大指数全线收涨"
    elif not up:
        mood, short = "四大指数本周全线收跌", "四大指数全线收跌"
    else:
        mood = f"四大指数本周涨跌互现，{len(up)} 涨 {4 - len(up)} 跌"
        short = f"{best['name']}领涨，指数分化" if best["week_chg"] > 0 else "四大指数涨跌互现"
    title = f"A股周记｜{short}"
    intro = f"{mood}。表现最强的是{best['name']}（{fmt_chg(best['week_chg'])}），最弱的是{worst['name']}（{fmt_chg(worst['week_chg'])}）。"
    obs = [
        f"强弱分化：{best['name']}与{worst['name']}本周涨跌幅相差 {best['week_chg'] - worst['week_chg']:.2f} 个百分点。",
        (f"均线位置：{('、'.join(above) + ' 站在 20 日均线上方') if above else '四个指数都在 20 日均线下方'}"
         f"{'' if len(above) in (0, 4) else '，其余仍在均线下方'}，短期趋势{'偏强' if len(above) >= 3 else '偏弱' if len(above) <= 1 else '分化'}。"),
        f"波动幅度：{max(data, key=lambda d: (d['week_high'] / d['week_low']))['name']}本周振幅最大，资金分歧相对更明显。",
    ]
    return title, intro, obs


def gemini_text(data):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    candidates = [os.getenv("GEMINI_MODEL"), os.getenv("GEMINI_MODEL_FALLBACK"),
                  "gemini-3-flash-preview", "gemini-3.8-flash"]
    models = list(dict.fromkeys(m for m in candidates if m))
    facts = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()} for d in data]
    prompt = (
        "你是一位克制、专业的财经专栏作者，为个人微信公众号写一篇《A股周记》。\n"
        "下面是 A股四大指数本周数据（week_chg 为本周涨跌幅%，ma20 为 20 日均线）：\n"
        f"{json.dumps(facts, ensure_ascii=False)}\n\n"
        "请只依据这些数据写作，不要编造新闻、政策或任何数据中没有的数字。\n"
        "严禁给出任何买入、卖出、加仓、减仓、持有等操作建议，也不要预测点位。\n"
        "输出 JSON：{\"title\": 标题(以'A股周记｜'开头,总长不超过 26 字), "
        "\"intro\": 导语(60-100字), \"observations\": [三条观察,每条 40-80 字,以一句加粗小标题开头,用'：'分隔]}\n"
        "只输出 JSON。"
    )
    for m in models:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                params={"key": key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json"}},
                timeout=60,
            )
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            j = json.loads(txt)
            if j.get("title") and j.get("intro") and len(j.get("observations", [])) >= 3:
                print(f"[info] Gemini 写作成功: {m}")
                return j["title"], j["intro"], j["observations"][:3]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Gemini {m}: {str(e)[:200]}")
    return None


# ---------------------------------------------------------------- 排版
TD = 'style="border:1px solid #e5e5e5;padding:8px 6px;text-align:center;font-size:13px;"'
TH = 'style="border:1px solid #e5e5e5;padding:8px 6px;text-align:center;font-size:14px;background:#f6f7f9;font-weight:bold;"'


def color(x):
    # A股习惯：红涨绿跌
    return "#d0021b" if x > 0 else "#1a9e4b" if x < 0 else "#333"


def build_html(data, title, intro, obs):
    start, end = data[0]["first_date"], data[0]["last_date"]
    rows = "".join(
        f"<tr><td {TD}>{d['name']}</td><td {TD}>{d['close']:.2f}</td>"
        f"<td {TD}><span style=\"color:{color(d['week_chg'])};font-weight:bold;\">{fmt_chg(d['week_chg'])}</span></td>"
        f"<td {TD}>{d['week_low']:.2f} – {d['week_high']:.2f}</td>"
        f"<td {TD}>{'上方' if d['above_ma20'] else '下方'}</td></tr>"
        for d in data
    )
    obs_html = ""
    for i, o in enumerate(obs, 1):
        head, _, rest = o.partition("：")
        if not rest:
            head, rest = "", o
        obs_html += (f'<p style="font-size:15px;line-height:1.9;margin:0 0 14px;">'
                     f'<strong>{i}. {head}{"：" if head else ""}</strong>{rest}</p>')
    body = f"""
<section style="font-size:15px;color:#333;line-height:1.8;">
<p style="color:#999;font-size:13px;margin:0 0 16px;">统计区间：{start} 至 {end} · AI 辅助整理</p>
<p style="font-size:15px;line-height:1.9;margin:0 0 18px;">{intro}</p>
<h3 style="font-size:17px;border-left:4px solid #b03a2e;padding-left:10px;margin:24px 0 12px;">一、本周四大指数</h3>
<table style="border-collapse:collapse;width:100%;"><tbody>
<tr><th {TH}>指数</th><th {TH}>周五收盘</th><th {TH}>本周涨跌</th><th {TH}>本周区间</th><th {TH}>20日均线</th></tr>
{rows}
</tbody></table>
<p style="color:#999;font-size:12px;margin:8px 0 0;">注：红涨绿跌；"20日均线"指收盘价位于 20 日均线上方或下方。</p>
<h3 style="font-size:17px;border-left:4px solid #b03a2e;padding-left:10px;margin:28px 0 12px;">二、三点观察</h3>
{obs_html}
<h3 style="font-size:17px;border-left:4px solid #b03a2e;padding-left:10px;margin:28px 0 12px;">三、写在最后</h3>
<p style="font-size:15px;line-height:1.9;">每周记一笔，过一段时间回头看，比任何预测都更能看清市场的节奏。</p>
<p style="color:#999;font-size:12px;line-height:1.7;margin-top:28px;">声明：本文数据来自公开行情，由 AI 工具辅助整理，仅为个人市场观察记录，不构成任何投资建议。股市有风险，投资需谨慎。</p>
</section>"""
    return body.strip()


def build_cover(data, title, path):
    from PIL import Image, ImageDraw, ImageFont

    W, H = 900, 383
    img = Image.new("RGB", (W, H), "#16263d")
    dr = ImageDraw.Draw(img)
    for y in range(H):  # 竖向渐变
        c = int(22 + 18 * y / H)
        dr.line([(0, y), (W, y)], fill=(c, 38 + int(20 * y / H), 61 + int(30 * y / H)))
    fonts = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ]
    fp = next((f for f in fonts if os.path.exists(f)), None)
    big = ImageFont.truetype(fp, 56) if fp else ImageFont.load_default()
    small = ImageFont.truetype(fp, 24) if fp else ImageFont.load_default()
    headline = title.split("｜", 1)[-1][:12]
    dr.text((56, 54), "A股周记 · 市场笔记", font=small, fill="#9fb8d6")
    if "，" in headline:
        l1, l2 = headline.split("，", 1)
    else:
        l1, l2 = headline[:6], headline[6:]
    dr.text((56, 110), l1, font=big, fill="white")
    dr.text((56, 182), l2, font=big, fill="white")
    dr.text((58, 310), f"{data[0]['first_date']} — {data[0]['last_date']}", font=small, fill="#c8d6e6")
    # 右侧：四个指数涨跌条
    x0, y0 = 560, 70
    for i, d in enumerate(data):
        y = y0 + i * 62
        dr.text((x0, y), d["name"], font=small, fill="#dfe8f2")
        dr.text((x0 + 150, y), fmt_chg(d["week_chg"]), font=small,
                fill="#ff6b6b" if d["week_chg"] > 0 else "#4fc38a")
    img.save(path)


# ---------------------------------------------------------------- 邮件
def send_mail(subject, html, cover_path):
    sender, pwd = os.getenv("EMAIL_SENDER"), os.getenv("EMAIL_PASSWORD")
    if not (sender and pwd):
        print("[warn] 未配置邮箱，跳过发送")
        return
    to = [x.strip() for x in (os.getenv("EMAIL_RECEIVERS") or sender).split(",") if x.strip()]
    domain = sender.split("@")[-1].lower()
    host, port = {"gmail.com": ("smtp.gmail.com", 465), "qq.com": ("smtp.qq.com", 465),
                  "163.com": ("smtp.163.com", 465)}.get(domain, ("smtp." + domain, 465))
    msg = MIMEMultipart("related")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to)
    guide = ('<p style="background:#fff8e1;padding:10px;font-size:13px;color:#8a6d3b;">'
             '使用方法：选中下方虚线框内的正文 → Ctrl+C → 粘贴到公众号编辑器正文；'
             '附件 cover.png 上传为封面。标题建议：<b>' + subject.replace("【公众号周记】", "") + '</b></p>'
             '<div style="border:1px dashed #ccc;padding:16px;">')
    msg.attach(MIMEText(guide + html + "</div>", "html", "utf-8"))
    with open(cover_path, "rb") as f:
        part = MIMEImage(f.read(), name="cover.png")
    part.add_header("Content-Disposition", "attachment", filename="cover.png")
    msg.attach(part)
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(sender, pwd)
        s.sendmail(sender, to, msg.as_string())
    print(f"[info] 公众号周记已发送到 {len(to)} 个邮箱")


def main():
    data = [summarize(c, n) for c, n in INDICES]
    print(json.dumps(data, ensure_ascii=False, indent=1))
    title, intro, obs = gemini_text(data) or rule_text(data)
    html = build_html(data, title, intro, obs)
    stamp = data[0]["last_date"]
    (OUT / f"wechat_{stamp}.html").write_text(html, encoding="utf-8")
    cover = OUT / f"cover_{stamp}.png"
    build_cover(data, title, cover)
    print(f"[info] 标题: {title}")
    if "--no-email" not in sys.argv:
        send_mail(f"【公众号周记】{title}", html, cover)


if __name__ == "__main__":
    main()
