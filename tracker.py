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
MIN_ANNUAL_PRICE = 5.00   # 年付最低合理价格（过滤 $3.95 等月付干扰）
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


def is_strictly_annual_price(card, text):
    """
    严格校验卡片的价格是否为【年付】且在 [$8.00, $15.00) 区间内
    返回: (is_valid_annual, price_float)
    """
    price_box = card.find(["div", "span", "p"], class_=re.compile(r"price|pricing", re.I))
    price_text = price_box.get_text(" ", strip=True) if price_box else text

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

    # 2. 检查年付标识
    annual_keywords = ["每年", "年付", "年度", "年缴", "/年", "1 年", "1年", "annually", "yearly", "per year", "/yr", "/year"]
    has_annual_flag = any(k in price_text_lower for k in annual_keywords) or any(k in text.lower() for k in annual_keywords)

    if not has_annual_flag:
        if any(k in text.lower() for k in non_annual_keywords):
            return False, 0.0

    # 3. 筛选符合 [$8.00, $15.00) 区间的年付价格
    valid_prices = [float(p) for p in price_matches if MIN_ANNUAL_PRICE <= float(p) < MAX_ANNUAL_PRICE]
    if not valid_prices:
        return False, 0.0

    return True, min(valid_prices)


def extract_plan_name(card):
