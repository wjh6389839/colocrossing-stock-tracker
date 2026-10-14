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
MIN_ANNUAL_PRICE = 8.00   # 年付最低合理价格（过滤 $3.95 等月付干扰）
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


def is_ strictly_annual_price(card, text):
    """
    严格校验卡片的价格是否为【年付】且在 [$8.00, $15.00) 区间内
    返回: (is_valid_annual, price_float)
    """
    # 优先定位 WHMCS 标准价格块区域，避免被商品描述里的文字干扰
    price_box = card.find(["div", "span", "p"], class_=re.compile(r"price|pricing", re.I))
    price_text = price_box.get_text(" ", strip=True) if price_box else text

    # 提取价格数字
    price_matches = re.findall(r'\$\s*(\d+\.\d{2})', price_text)
    if not price_matches:
        price_matches = re.findall(r'\$\s*(\d+\.\d{2})', text)
    if not price_matches:
        return False, 0.0

    # 1. 严格排除月付、季付、半年付等非年付周期词汇
    non_annual_keywords = [
        "每月", "月付", "月度", "月缴", "/月", "monthly", "per month", "/mo", "1 month",
        "每季", "季付", "季度", "quarterly",
        "半年", "semi-annually"
    ]
    price_text_lower = price_text.lower()
    if any(k in price_text_lower for k in non_annual_keywords):
        return False, 0.0

    # 2. 必须包含明确的年付标识（或者通过正则匹配“$价格 + 年付周期”紧邻结构）
    annual_keywords = ["每年", "年付", "年度", "年缴", "/年", "1 年", "1年", "annually", "yearly", "per year", "/yr", "/year"]
    has_annual_flag = any(k in price_text_lower for k in annual_keywords) or any(k in text.lower() for k in annual_keywords)

    # 如果整个卡片连“年/annually”字样都没有，或者全文带有“每月/月付”但没有年付，直接排除
    if not has_annual_flag:
        if any(k in text.lower() for k in non_annual_keywords):
            return False, 0.0

    # 3. 筛选符合 [$8.00, $15.00) 区间的年付价格
    valid_prices = [float(p) for p in price_matches if MIN_ANNUAL_PRICE <= float(p) < MAX_ANNUAL_PRICE]
    if not valid_prices:
        return False, 0.0

    return True, min(valid_prices)


def extract_plan_name(card):
    """精准提取套餐名称，杜绝空名称"""
    # 依次尝试 WHMCS 常见的商品标题标签与 ID（如 id="product1-name"）
    selectors = [
        "header h3", "header h4", "header span",
        "[id$='-name']", ".product-title", ".package-title",
        "h3", "h4", "h5", "strong"
    ]
    for sel in selectors:
        tag = card.select_one(sel)
        if tag:
            name = tag.get_text(" ", strip=True)
            # 过滤掉纯价格或过短的无意义字符
            if name and not name.startswith("$") and len(name) > 2:
                return name
    return ""


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
    # 仅选择最内层的单个商品卡片，排除外层 id="products" 列表总容器
    raw_cards = soup.select("div.product, div.package, div.price-table, div.panel-product")
    if not raw_cards:
        # 备用选择器：只匹配 product 加数字的 ID（如 product1, product2），排除 products
        raw_cards = [div for div in soup.find_all("div", id=re.compile(r"^product\d+$"))]

    low_price_plans = []
    seen_identifiers = set()  # 用于去重，防止嵌套 div 重复抓取同一套餐
    out_of_stock_keywords = ["out of stock", "缺货", "售罄", "unavailable", "0 available", "已售完"]

    for card in raw_cards:
        # 如果该容器内部还包含其他子商品卡片，说明它是外层包裹容器，直接跳过
        if len(card.select("div.product, div.package")) > 0:
            continue

        text = card.get_text(" ", strip=True)
        is_annual, price = is_strictly_annual_price(card, text)
        if not is_annual:
            continue

        plan_name = extract_plan_name(card)
        if not plan_name:
            continue  # 如果连套餐名都提取不到，说明是无效碎片容器，直接跳过

        # 提取购买链接
        order_link = ""
        a_tag = card.find("a", href=re.compile(r"cart\.php.*a=add|store/.*|rp=/store/", re.I))
        if not a_tag:
            a_tag = card.find("a", class_=re.compile(r"btn|order|buy", re.I))

        if a_tag and a_tag.get("href"):
            href = a_tag["href"]
            order_link = href if href.startswith("http") else (BASE_URL + "/" + href.lstrip("/"))

        # 根据“套餐名 + 购买链接”去重
        unique_key = f"{plan_name}_{order_link}"
        if unique_key in seen_identifiers:
            continue
        seen_identifiers.add(unique_key)

        # 1. 检查卡片上是否直接标明缺货
        in_stock = True
        if any(k in text.lower() for k in out_of_stock_keywords):
            in_stock = False

        # 2. 进入二级购物车页面确认是否真有货
        if order_link and in_stock:
            try:
                sub_resp = fetch_html(order_link, browser=active_browser)
                if sub_resp.status_code == 200:
                    sub_text = sub_resp.text.lower()
                    if any(k in sub_text for k in out_of_stock_keywords) or "out-of-stock" in sub_text:
                        in_stock = False
            except Exception as e:
                print(f"⚠️ 二级页面验货异常 ({plan_name}): {e}")
        elif not order_link and not card.find("a"):
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
        new_rows.append([time_str, "否 (无<$15年付套餐)", "-", "-", "🔴 缺货/未上架", TARGET_URL])
        print("📝 已记录：当前页面无低于 $15/年 的套餐。")
    else:
        for p in plans:
            status_str = "🟢 有货" if p["in_stock"] else "🔴 缺货"
            new_rows.append([time_str, "是", p["name"], f"${p['price']:.2f}/年", status_str, p["link"]])
            print(f"📝 已记录: {p['name']} (${p['price']:.2f}/年) -> {status_str}")

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
                history_lines.reverse()

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
                f.write(f"| {p['name']} | **${p['price']:.2f}/年** | {badge} | [点击直达]({p['link']}) |\n")
            f.write("\n")

        f.write("## 📜 最近 20 条检查记录（完整记录见 `stock_history.csv`）\n\n")
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
        lines.append(f"| **{p['name']}**
