"""爬取中关村在线DIY配件频道的产品列表和详细参数，输出 JSONL"""

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).parent.parent.parent
MAX_PAGES = 3

# 产品名称可能被 h3 里的参数 span 污染："AMD Ryzen 5 5600 适用类型:笔记本 ..."
_PARAM_SUFFIX_RE = re.compile(r"\s{1,2}\S+?:.+$", re.DOTALL)


def _clean_title(raw: str) -> str:
    return _PARAM_SUFFIX_RE.sub("", raw).strip()


CATEGORIES = {
    "cpu": {
        "detail_prefix": "/cpu/",
        "brands": [
            ("Intel", "https://detail.zol.com.cn/cpu/intel/"),
            ("AMD", "https://detail.zol.com.cn/cpu/amd/"),
        ],
    },
    "motherboard": {
        "detail_prefix": "/motherboard/",
        "url": "https://detail.zol.com.cn/motherboard/",
    },
    "vga": {
        "detail_prefix": "/vga/",
        "url": "https://detail.zol.com.cn/vga/",
    },
    "memory": {
        "detail_prefix": "/memory/",
        "url": "https://detail.zol.com.cn/memory/",
    },
    "solid_state_drive": {
        "detail_prefix": "/series/",
        "url": "https://detail.zol.com.cn/solid_state_drive/",
    },
    "hard_drives": {
        "detail_prefix": "/hard_drives/",
        "url": "https://detail.zol.com.cn/hard_drives/",
    },
    "power": {
        "detail_prefix": "/power/",
        "url": "https://detail.zol.com.cn/power/",
    },
    "case": {
        "detail_prefix": "/case/",
        "url": "https://detail.zol.com.cn/case/",
    },
    "cooling_product": {
        "detail_prefix": "/cooling_product/",
        "url": "https://detail.zol.com.cn/cooling_product/",
    },
}


def extract_card_info(card, prefix):
    """从列表页的一张卡片取基础信息，推广链接返 None"""
    data_id = card.get_attribute("data-follow-id")
    link = card.locator("a").first.get_attribute("href")
    if not link:
        return None

    if not link.startswith(f"{prefix}"):
        return None

    title_el = card.locator("h3 a").first
    if title_el.count() == 0:
        title_el = card.locator("h3").first
    title = _clean_title(title_el.text_content().strip()) if title_el else ""

    price_el = card.locator("b.price-type")
    price = price_el.first.text_content().strip() if price_el.count() > 0 else None

    img_el = card.locator("img").first
    img = img_el.get_attribute("src") or img_el.get_attribute("data-src")

    return {
        "产品ID": data_id,
        "产品名称": title,
        "参考价格": price,
        "缩略图URL": img,
        "详情链接": link,
        "params": {},
    }


def scrape_params(detail_page, data_page, product_url):
    """进产品页 → 找参数页链接 → 进参数页 → 拉所有参数表"""
    try:
        data_page.goto(product_url, timeout=30000, wait_until="domcontentloaded")

        more_btn = data_page.locator("a:has-text('查看完整参数')")
        if more_btn.count() == 0:
            more_btn = data_page.locator("a:has-text('更多参数')")
        if more_btn.count() == 0:
            # 尝试笔记本的 selector 作为兜底
            more_btn = data_page.locator("a._j_MP_more.section-more")

        if more_btn.count() == 0:
            return {}

        detail_path = more_btn.first.get_attribute("href")
        if not detail_path:
            return {}

        if detail_path.startswith("//"):
            detail_url = f"https:{detail_path}"
        else:
            detail_url = f"https://detail.zol.com.cn{detail_path}"

        detail_page.goto(detail_url, timeout=30000, wait_until="domcontentloaded")
        detail_page.wait_for_selector("div.detailed-parameters", timeout=10000)

        tables = detail_page.locator("div.detailed-parameters").locator("table")
        detailed_dict = {}

        for table_idx in range(tables.count()):
            table = tables.nth(table_idx)
            params = {}
            category = ""
            rows = table.locator("tr")
            for r_idx in range(rows.count()):
                row = rows.nth(r_idx)
                # 标题
                hd = row.locator("td.hd")
                if hd.count() > 0:
                    category = hd.first.text_content().strip()
                    continue

                th, td = row.locator("th"), row.locator("td")
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


if __name__ == "__main__":
    import sys

    # 支持命令行指定品类: python components.py cpu
    selected = sys.argv[1] if len(sys.argv) > 1 else None
    if selected and selected not in CATEGORIES:
        print(f"未知品类: {selected}，可选: {list(CATEGORIES.keys())}")
        sys.exit(1)
    targets = {selected: CATEGORIES[selected]} if selected else CATEGORIES

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",  # noqa: E501
            viewport={"width": 1920, "height": 1080},
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en']
            });
        """)

        for category, cfg in targets.items():
            if "brands" in cfg:  # type: ignore[operator]
                urls = [(brand_name, brand_url) for brand_name, brand_url in cfg["brands"]]  # type: ignore[index]
            else:
                urls = [(category, cfg["url"])]  # type: ignore[index]

            for name, start_url in urls:
                print(f"\n{'=' * 50}")
                print(f"开始爬取: {name}")
                print(f"{'=' * 50}")

                save_path = (
                    root
                    / "data"
                    / "products"
                    / "raw"
                    / "components"
                    / category
                    / f"{name}_{category}.jsonl"
                )
                save_path.parent.mkdir(parents=True, exist_ok=True)

                page = context.new_page()
                data_page = context.new_page()
                detail_page = context.new_page()

                f = open(save_path, "w", encoding="utf-8")

                cur_url = start_url

                for page_num in range(1, MAX_PAGES + 1):
                    print(f"  [第 {page_num} 页]")
                    try:
                        page.goto(cur_url, timeout=30000, wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                    except Exception as e:
                        print(f"  列表页加载失败: {e}  URL: {cur_url}")
                        page.screenshot(path=f"/tmp/zol_debug_p{page_num}.png")
                        break

                    cards = page.locator("li[data-follow-id]")
                    card_count = cards.count()
                    if card_count == 0:
                        cards = page.locator(f"li:has(a[href*='{cfg['detail_prefix']}'])")  # type: ignore[index]
                        card_count = cards.count()
                    print(f"  找到 {card_count} 个产品  URL: {cur_url}")

                    for card_idx in range(card_count):
                        card = cards.nth(card_idx)
                        product: dict = extract_card_info(card, cfg["detail_prefix"])  # type: ignore[index]
                        if product is None:
                            print(f"    [{card_idx + 1}] 跳过推广")
                            continue

                        product_url = f"https://detail.zol.com.cn{product['详情链接']}"
                        product["params"] = scrape_params(detail_page, data_page, product_url)

                        f.write(json.dumps(product, ensure_ascii=False) + "\n")
                        name_preview = product["产品名称"][:40] if product["产品名称"] else "?"
                        print(f"    [{card_idx + 1}] {name_preview}...")

                    # 翻页
                    next_btn = page.locator(".pagebar a.next")
                    if next_btn.count() == 0:
                        print("  没有下一页了")
                        break
                    cur_url = f"https://detail.zol.com.cn{next_btn.first.get_attribute('href')}"

                f.close()
                page.close()
                data_page.close()
                detail_page.close()
                print(f"{name} 完成，文件: {save_path}")

        context.close()
        browser.close()
    print("\n全部完成!")
