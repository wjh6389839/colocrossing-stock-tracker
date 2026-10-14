import os
import re
import sys
import csv
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
import requests

# ================= 基础配置 =================
TARGET_URL = "https://cloud.colocrossing.com/index.php?rp=%2Fstore%2Fspecials&language=chinese"
BASE_URL = "https://cloud.colocrossing.com"
MAX_ANNUAL_PRICE = 15.00  # 监控年付低于 $15 的套餐
CSV_FILE = "stock_history.csv"
README_FILE = "README.md"
SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
# ============================================


def get_beijing_time():
    """获取北京时间"""
    return datetime.now(timezone.utc) + timedelta(hours=8)


def fetch_html(url, browser="chrome124"):
    """使用 curl_cffi 模拟真实 Chrome 指纹绕过 Cloudflare 403"""
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://cloud.colocrossing.com/",
        "Cache-Control": "no-cache",
    }
    return cffi_requests.get(url, impersonate=browser, headers=headers, timeout=30)


def parse_and_check_stock():
    """解析特惠页面并深入二级链接验证低于 $15/年 的套餐是否有货"""
    browsers = ["chrome124", "chrome120", "chrome116", "safari17_0", "edge101"]
    html = None
    active_browser = "chrome124"

    for b in browsers:
        try:
            resp = fetch_html(TARGET_URL, browser=b)
            print(f"[{datetime.now()}] ({b}) 网页响应状态码: {resp.status_code}")
            if resp.status_code == 200 and "Just a moment" not in resp.text:
                html = resp.text
                active_browser = b
                break
        except Exception as e:
            print(f"⚠️ {b} 请求异常: {e}")

    if not html:
        print("❌ 抓取页面异常: 所有浏览器指纹均被拦截或访问失败！")
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")
    # 匹配 WHMCS 商品卡片容器
    cards = soup.select("div.product, div.package, div.price-table, div.panel-product, div[id^='product']")

    low_price_plans = []
    out_of_stock_keywords = ["out of stock", "缺货", "售罄", "unavailable", "0 available", "已售完"]

    for card in cards:
        text = card.get_text(" ", strip=True)
        # 提取形如 $10.00 或 $ 12.95 的价格
        price_matches = re.findall(r'\$\s*(\d+\.\d{2})', text)
        if not price_matches:
            continue

        # 筛选低于 15 美元的价格（排除低于 $3 的域名或附加插件）
        valid_prices = [float(p) for p in price_matches if 3.0 <= float(p) < MAX_ANNUAL_PRICE]
        if not valid_prices:
            continue

        # 判断是否为年付（如果明确只写了月付 Month 且没提年付，则跳过）
        text_lower = text.lower()
        is_annual = any(k in text_lower for k in ["year", "annually", "年", "yr", "/y"])
        if not is_annual and ("month" in text_lower or "月" in text_lower):
            continue

        price = min(valid_prices)

        # 提取套餐标题名称
        title_tag = card.find(["h3", "h4", "h5", "header", "strong", "span"], class_=re.compile(r"title|name|heading", re.I))
        if not title_tag:
            title_tag = card.find(["h3", "h4", "h5", "strong"])
        plan_name = title_tag.get_text(strip=True) if title_tag else f"Special VPS (${price:.2f}/yr)"

        # 1. 检查卡片上是否直接标明缺货
        in_stock = True
        if any(k in text_lower for k in out_of_stock_keywords):
            in_stock = False

        # 2. 提取立即购买链接，并进入二级购物车页面确认是否真有货
        order_link = ""
        a_tag = card.find("a", href=re.compile(r"cart\.php.*a=add|store/.*|rp=/store/", re.I))
        if not a_tag:
            a_tag = card.find("a", class_=re.compile(r"btn|order|buy", re.I))

        if a_tag and a_tag.get("href"):
            href = a_tag["href"]
            order_link = href if href.startswith("http") else (BASE_URL + "/" + href.lstrip("/"))

            if in_stock:
                try:
                    sub_resp = fetch_html(order_link, browser=active_browser)
                    if sub_resp.status_code == 200:
                        sub_text = sub_resp.text.lower()
                        if any(k in sub_text for k in out_of_stock_keywords) or "out-of-stock" in sub_text:
                            in_stock = False
                except Exception as e:
                    print(f"⚠️ 二级页面验货异常 ({plan_name}): {e}")
        else:
            if not card.find("a"):
                in_stock = False

        low_price_plans.append({
            "name": plan_name,
            "price": price,
            "in_stock": in_stock,
            "link": order_link or TARGET_URL
        })

    return low_price_plans


def record_data(time_str, plans):
    """记录到 CSV 历史表，并同步更新仓库首页 README.md"""
    file_exists = os.path.exists(CSV_FILE)
    new_rows = []

    if not plans:
        new_rows.append([time_str, "否 (无<$15套餐)", "-", "-", "🔴 缺货/未上架", TARGET_URL])
        print("📝 已记录：当前页面无低于 $15/年 的套餐。")
    else:
        for p in plans:
            status_str = "🟢 有货" if p["in_stock"] else "🔴 缺货"
            new_rows.append([time_str, "是", p["name"], f"${p['price']:.2f}", status_str, p["link"]])
            print(f"📝 已记录：{p['name']} (${p['price']:.2f}/年) -> {status_str}")

    with open(CSV_FILE, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["检查时间(北京时间)", "存在<$15套餐", "套餐名称", "年付价格", "库存状态", "购买链接"])
        writer.writerows(new_rows)

    # 读取最近 20 条历史记录展示在 README 首页
    history_lines = []
    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="r", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
            if len(reader) > 1:
                history_lines = reader[1:][-20:]
                history_lines.reverse()  # 最新记录排在最前面

    # 生成 README.md 看板
    with open(README_FILE, mode="w", encoding="utf-8") as f:
        f.write("# 🖥️ ColoCrossing 特惠 VPS (< $15/年) 库存监控\n\n")
        f.write(f"- **最后检查时间（北京时间）**：`{time_str}`\n")
        f.write(f"- **监控目标**：[ColoCrossing 特别优惠页]({TARGET_URL})（年付 < `${MAX_ANNUAL_PRICE:.2f} USD`）\n\n")

        f.write("## 📊 今日最新状态\n\n")
        if not plans:
            f.write("> 🔴 **当前无货**：页面上暂未发现年付低于 $15 的特惠套餐（已下架或隐藏）。\n\n")
        else:
            f.write("| 套餐名称 | 年付价格 | 库存状态 | 购买链接 |\n")
            f.write("| :--- | :--- | :--- | :--- |\n")
            for p in plans:
                badge = "🟢 **有货 (可购买)**" if p["in_stock"] else "🔴 缺货 (Out of Stock)"
                f.write(f"| {p['name']} | **${p['price']:.2f}** | {badge} | [点击直达]({p['link']}) |\n")
            f.write("\n")

        f.write("## 📜 最近 20 次检查记录（完整记录见 `stock_history.csv`）\n\n")
        f.write("| 检查时间 (北京时间) | 套餐名称 | 年付价格 | 库存状态 |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        for r in history_lines:
            f.write(f"| `{r[0]}` | {r[2]} | {r[3]} | {r[4]} |\n")


def send_wechat_alert(in_stock_plans, time_str):
    """当有货时通过 Server酱 发送微信提醒"""
    if not SENDKEY:
        print("ℹ️ 未配置 SERVERCHAN_SENDKEY，跳过微信推送。")
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
    desp = "\n".join(lines)

    try:
        res = requests.post(
            f"https://sctapi.ftqq.com/{SENDKEY}.send",
            data={"title": title, "desp": desp},
            timeout=15
        )
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
        print(f"🎉 发现 {len(in_stock_plans)} 款低于 $15/年 的有货套餐，正在发送微信提醒...")
        send_wechat_alert(in_stock_plans, bj_time_str)
    else:
        print("☕ 当前无低于 $15/年 的有货套餐，已记录到历史表，本次不发送微信提醒。")
