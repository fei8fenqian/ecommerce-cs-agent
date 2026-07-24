import json
from pathlib import Path

from playwright.sync_api import sync_playwright

# ====== 配置 ======
root = Path(__file__).parent.parent
MAX_PAGES = 3

BRANDS = [
    ("联想", "https://detail.zol.com.cn/notebook_index/subcate16_160_list_1.html"),
    ("华硕", "https://detail.zol.com.cn/notebook_index/subcate16_227_list_1.html"),
    ("惠普", "https://detail.zol.com.cn/notebook_index/subcate16_223_list_1.html"),
    ("戴尔", "https://detail.zol.com.cn/notebook_index/subcate16_21_list_1.html"),
    ("华为", "https://detail.zol.com.cn/notebook_index/subcate16_613_list_1.html"),
    ("Acer宏碁", "https://detail.zol.com.cn/notebook_index/subcate16_218_list_1.html"),
    ("小米", "https://detail.zol.com.cn/notebook_index/subcate16_34645_list_1.html"),
    ("苹果", "https://detail.zol.com.cn/notebook_index/subcate16_544_list_1.html"),
    ("荣耀", "https://detail.zol.com.cn/notebook_index/subcate16_50840_list_1.html"),
    (
        "机械革命",
        "https://detail.zol.com.cn/notebook_index/subcate16_35578_list_1.html",
    ),
    ("微星", "https://detail.zol.com.cn/notebook_index/subcate16_133_list_1.html"),
    ("神舟", "https://detail.zol.com.cn/notebook_index/subcate16_1191_list_1.html"),
]


def extract_card_info(card):
    """从列表页的一张卡片取基础信息，推广链接返 None"""
    data_id = card.get_attribute("data-follow-id")

    link = card.locator("a").first.get_attribute("href")
    if not link:
        return None

    # 过滤推广页
    if not link.startswith("/notebook/") and not link.startswith("/ultrabook/"):
        return None

    title = card.locator("span").locator("a").first.text_content()

    price_el = card.locator("b.price-type")
    price = price_el.first.text_content() if price_el.count() > 0 else None

    img_el = card.locator("img").first
    img = img_el.get_attribute("src") or img_el.get_attribute(".src")

    return {
        "产品ID": data_id,
        "产品名称": title,
        "参考价格": price,
        "缩略图URL": img,
        "详情链接": link,
        "params": {},
    }


def scrape_params(detail_page, data_page, product_url):
    """进产品页 → 找参数页链接 → 进参数页 → 抓参数"""
    try:
        # 进产品页，找参数页链接
        data_page.goto(product_url, timeout=30000, wait_until="domcontentloaded")
        data_page.wait_for_selector("text=查看完整参数", timeout=10000)

        param_path = data_page.locator("a._j_MP_more.section-more").first.get_attribute("href")
        if not param_path:
            return {}

        if param_path.startswith("//"):
            param_url = f"https:{param_path}"
        else:
            param_url = f"https://detail.zol.com.cn{param_path}"

        # 进参数页
        detail_page.goto(param_url, timeout=30000, wait_until="domcontentloaded")
        detail_page.wait_for_selector("div.detailed-parameters", timeout=10000)

        # 抓所有参数表
        tables = detail_page.locator("div.detailed-parameters").locator("table")
        detailed_dict = {}

        for tbl_idx in range(tables.count()):
            params = {}
            rows = tables.nth(tbl_idx).locator("tr")
            category = ""

            for r in range(rows.count()):
                row = rows.nth(r)

                # 分类标题行
                hd = row.locator("td.hd")
                if hd.count() > 0:
                    category = hd.first.text_content().strip()
                    continue

                # 参数行
                th = row.locator("th")
                td = row.locator("td")
                if th.count() == 0 or td.count() == 0:
                    continue

                param_name = th.first.text_content().strip().replace("问豆包", "").strip()
                param_val = td.locator('span[id^="newPmVal"]').first.text_content().strip()
                params[param_name] = param_val

            if category and params:
                detailed_dict[category] = params

        return detailed_dict

    except Exception as e:
        print(f"    参数抓取失败: {e}")
        return {"error": str(e)}


# ====== 主流程 ======
if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for brand_name, brand_url in BRANDS:
            print(f"\n{'=' * 50}")
            print(f"开始爬取: {brand_name}")
            print(f"{'=' * 50}")

            # 创建存储文件
            save_path = (
                root / "data" / "products" / "raw" / "laptops" / f"{brand_name}_laptops.jsonl"
            )
            save_path.parent.mkdir(parents=True, exist_ok=True)

            # 开标签页
            page = browser.new_page()
            data_page = browser.new_page()
            detail_page = browser.new_page()

            f = open(save_path, "w", encoding="utf-8")
            current_url = brand_url

            for page_num in range(1, MAX_PAGES + 1):
                print(f"  [第 {page_num} 页]")

                try:
                    page.goto(current_url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_selector("li[data-follow-id]", timeout=10000)
                except Exception as e:
                    print(f"  列表页加载失败: {e}")
                    break

                cards = page.locator("li[data-follow-id]")
                card_count = cards.count()
                print(f"  找到 {card_count} 个产品")

                for card_idx in range(card_count):
                    card = cards.nth(card_idx)

                    # 取基础信息
                    product = extract_card_info(card)
                    if product is None:
                        print(f"    [{card_idx + 1}] 跳过推广")
                        continue

                    # 抓参数
                    product_url = f"https://detail.zol.com.cn{product['详情链接']}"
                    product["params"] = scrape_params(detail_page, data_page, product_url)

                    # 保存一行 JSONL
                    f.write(json.dumps(product, ensure_ascii=False) + "\n")
                    print(f"    [{card_idx + 1}] {product['产品名称'][:30]}...")

                # 翻页
                next_btn = page.locator(".pagebar a.next")
                if next_btn.count() == 0:
                    print("  没有下一页了")
                    break
                current_url = f"https://detail.zol.com.cn{next_btn.first.get_attribute('href')}"

            f.close()
            page.close()
            data_page.close()
            detail_page.close()
            print(f"{brand_name} 完成，文件: {save_path}")

        browser.close()

    print("\n全部完成!")
