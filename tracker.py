import os
import re
import sys
import csv
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import requests

TARGET_URL = "https://cloud.colocrossing.com/index.php?rp=%2Fstore%2Fspecials&language=chinese"
BASE_URL = "https://cloud.colocrossing.com"
MIN_ANNUAL_PRICE = 5.00
MAX_ANNUAL_PRICE = 15.00
CSV_FILE = "stock_history.csv"
README_FILE = "README.md"
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")

def get_beijing_time():
    return datetime.now(timezone.utc) + timedelta(hours=8)

def fetch_html(url, browser="chrome124"):
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://cloud.colocrossing.com/",
        "Cache-Control": "no-cache",
    }
    return cffi_requests.get(url, impersonate=browser, headers=headers, timeout=30)

def is_strictly_annual_price(card, text):
    price_box = card.find(["div", "span", "p"], class_=re.compile(r"price|pricing", re.I))
    price_text = price_box.get_text(" ", strip=True) if price_box else text
    price_matches = re.findall(r'\$\s*(\d+\.\d{2})', price_text)
    if not price_matches:
        price_matches = re.findall(r'\$\s*(\d+\.\d{2})', text)
    if not price_matches:
        return False, 0.0
    non_annual = ["每月", "月付", "月度", "月缴", "/月", "monthly", "per month", "/mo", "1 month", "每季", "季付", "季度", "quarterly", "半年", "semi-annually"]
    p_lower = price_text.lower()
    if any(k in p_lower for k in non_annual):
        return False, 0.0
    annual_kw = ["每年", "年付", "年度", "年缴", "/年", "1 年", "1年", "annually", "yearly", "per year", "/yr", "/year"]
    has_annual = any(k in p_lower for k in annual_kw) or any(k in text.lower() for k in annual_kw)
    if not has_annual and any(k in text.lower() for k in non_annual):
        return False, 0.0
    valid_p = [float(p) for p in price_matches if MIN_ANNUAL_PRICE <= float(p) < MAX_ANNUAL_PRICE]
    if not valid_p:
        return False, 0.0
    return True, min(valid_p)

def extract_plan_name(card):
    selectors = ["header h3", "header h4", "header span", "[id$='-name']", ".product-title", ".package-title", "h3", "h4", "h5", "strong"]
    for sel in selectors:
        tag = card.select_one(sel)
        if tag:
            name = tag.get_text(" ", strip=True)
            if name and not name.startswith("$") and len(name) > 2:
                return name
    return ""

def parse_and_check_stock():
    browsers = ["chrome124", "chrome120", "chrome116", "safari17_0", "edge101"]
    html = None
    active_b = "chrome124"
    for b in browsers:
        try:
            resp = fetch_html(TARGET_URL, browser=b)
            print(f"[{datetime.now()}] ({b}) 网页响应状态码: {resp.status_code}")
            if resp.status_code == 200 and "Just a moment" not in resp.text:
                html = resp.text
                active_b = b
                break
        except Exception as e:
            print(f"请求异常 ({b}): {e}")
    if not html:
        print("抓取页面异常: 所有浏览器指纹均被拦截！")
        sys.exit(1)
    soup = BeautifulSoup(html, "html.parser")
    raw_cards = soup.select("div.product, div.package, div.price-table, div.panel-product")
    if not raw_cards:
        raw_cards = [div for div in soup.find_all("div", id=re.compile(r"^product\d+$"))]
    plans = []
    seen = set()
    oos_kw = ["out of stock", "缺货", "售罄", "unavailable", "0 available", "已售完"]
    for card in raw_cards:
        if len(card.select("div.product, div.package")) > 0:
            continue
        text = card.get_text(" ", strip=True)
        is_ann, price = is_strictly_annual_price(card, text)
        if not is_ann:
            continue
        plan_name = extract_plan_name(card)
        if not plan_name:
            continue
        order_link = ""
        a_tag = card.find("a", href=re.compile(r"cart\.php.*a=add|store/.*|rp=/store/", re.I))
        if not a_tag:
            a_tag = card.find("a", class_=re.compile(r"btn|order|buy", re.I))
        if a_tag and a_tag.get("href"):
            href = a_tag["href"]
            order_link = href if href.startswith("http") else (BASE_URL + "/" + href.lstrip("/"))
        ukey = f"{plan_name}_{order_link}"
        if ukey in seen:
            continue
        seen.add(ukey)
        in_stock = True
        if any(k in text.lower() for k in oos_kw):
            in_stock = False
        if order_link and in_stock:
            try:
                sub = fetch_html(order_link, browser=active_b)
                if sub.status_code == 200:
                    st = sub.text.lower()
                    if any(k in st for k in oos_kw) or "out-of-stock" in st:
                        in_stock = False
            except Exception as e:
                print(f"二级页面验货异常: {e}")
        elif not order_link and not card.find("a"):
            in_stock = False
        plans.append({"name": plan_name, "price": price, "in_stock": in_stock, "link": order_link or TARGET_URL})
    return plans

def record_data(time_str, plans):
    f_exists = os.path.exists(CSV_FILE)
    new_rows = []
    if not plans:
        new_rows.append([time_str, "否", "-", "-", "🔴 缺货/未上架", TARGET_URL])
        print("已记录：当前页面无低于 $15/年 的套餐。")
    else:
        for p in plans:
            st = "🟢 有货" if p["in_stock"] else "🔴 缺货"
            new_rows.append([time_str, "是", p["name"], f"${p['price']:.2f}/年", st, p["link"]])
            print(f"📝 已记录: {p['name']} (${p['price']:.2f}/年) -> {st}")
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if not f_exists:
            w.writerow(["检查时间(北京时间)", "存在<$15套餐", "套餐名称", "年付价格", "库存状态", "购买链接"])
        w.writerows(new_rows)
    hist = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="r", encoding="utf-8-sig") as f:
            rd = list(csv.reader(f))
            if len(rd) > 1:
                hist = rd[1:][-20:]
                hist.reverse()
    with open(README_FILE, mode="w", encoding="utf-8") as f:
        f.write("# 🖥️ ColoCrossing 特惠 VPS (< $15/年) 库存监控\n\n")
        f.write(f"- **最后检查时间（北京时间）**：`{time_str}`\n")
        f.write(f"- **监控目标**：[ColoCrossing 特别优惠页]({TARGET_URL})（年付 < `${MAX_ANNUAL_PRICE:.2f} USD`）\n\n")
        f.write("## 📊 今日最新状态\n\n")
        if not plans:
            f.write("> 🔴 **当前无货**：页面上暂未发现年付低于 $15 的特惠套餐。\n\n")
        else:
            f.write("| 套餐名称 | 年付价格 | 库存状态 | 购买链接 |\n| :--- | :--- | :--- | :--- |\n")
            for p in plans:
                bdg = "🟢 **有货 (可购买)**" if p["in_stock"] else "🔴 缺货 (Out of Stock)"
                f.write(f"| {p['name']} | **${p['price']:.2f}/年** | {bdg} | [点击直达]({p['link']}) |\n")
            f.write("\n")
        f.write("## 📜 最近 20 条检查记录（完整记录见 `stock_history.csv`）\n\n")
        f.write("| 检查时间 (北京时间) | 套餐名称 | 年付价格 | 库存状态 |\n| :--- | :--- | :--- | :--- |\n")
        for r in hist:
            f.write(f"| `{r[0]}` | {r[2]} | {r[3]} | {r[4]} |\n")

def send_wechat_alert(in_stock_plans, time_str):
    if not SENDKEY:
        return
    lowest_p = min(p["price"] for p in in_stock_plans)
    title = f"🎉 ColoCrossing 低于$15神机有货！最低 ${lowest_p:.2f}/年"
    lines = [
        f"### 🚨 监控到 ColoCrossing 出现低于 ${MAX_ANNUAL_PRICE:.0f}/年 的有货 VPS！",
        f"**检查时间**：`{time_str}`\n",
        "| 套餐名称 | 年付价格 | 状态 | 抢购链接 |",
        "| :--- | :--- | :--- | :--- |"
    ]
    for p in in_stock_plans:
        lines.append(f"| **{p['name']}** | `${p['price']:.2f}/年` | 🟢 有货 | [👉 立即抢购]({p['link']}) |")
    lines.append(f"\n[🔗 点击前往 ColoCrossing 特惠页面]({TARGET_URL})")
    try:
        res = requests.post(f"https://sctapi.ftqq.com/{SENDKEY}.send", data={"title": title, "desp": "\n".join(lines)}, timeout=15)
        print(f"📨 微信提醒推送结果: {res.text}")
    except Exception as e:
        print(f"⚠️ 微信推送异常: {e}")

if __name__ == "__main__":
    bj_time_str = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    print(f"🚀 开始执行 ColoCrossing 库存监控 ({bj_time_str})...")
    plans = parse_and_check_stock()
    record_data(bj_time_str, plans)
    in_stock_plans = [p for p in plans if p["in_stock"]]
    if in_stock_plans:
        send_wechat_alert(in_stock_plans, bj_time_str)
    else:
        print("☕ 当前无低于 $15/年 的有货套餐，已记录到历史表，本次不发送微信提醒。")
