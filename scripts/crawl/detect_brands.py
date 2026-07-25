"""探测 ZOL 各配件品类的产品选择器和品牌子页面，输出诊断信息"""

from playwright.sync_api import sync_playwright

TARGETS = {
    "cpu": {
        "detail_prefix": "/cpu/",
        "url": "https://detail.zol.com.cn/cpu/",
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
        "detail_prefix": "/solid_state_drive/",
        "url": "https://detail.zol.com.cn/solid_state_drive/",
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

if __name__ == "__main__":
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
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        page = context.new_page()

        for category, cfg in TARGETS.items():
            url = cfg["url"]
            prefix = cfg["detail_prefix"]
            print(f"\n{'=' * 60}")
            print(f"品类: {category}")
            print(f"URL:  {url}")
            print(f"前缀: {prefix}")
            print(f"{'=' * 60}")

            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"  ❌ 页面加载失败: {e}")
                continue

            title = page.title()
            print(f"  标题: {title[:80]}")

            # --- 产品检测 ---
            # 策略1: li[data-follow-id]
            cards1 = page.locator("li[data-follow-id]")
            c1 = cards1.count()
            print(f"  li[data-follow-id]: {c1} 个")

            # 策略2: li:has(a[href*='前缀'])
            cards2 = page.locator(f"li:has(a[href*='{prefix}'])")
            c2 = cards2.count()
            print(f"  li:has(a[href*='{prefix}']): {c2} 个")

            # 策略3: 其他常见选择器
            for sel in [
                ".pro-intro-list li",
                "ul.product-list li",
                "div.list-box li",
                "li[class*='product']",
                "div.item-box",
            ]:
                c = page.locator(sel).count()
                if c > 0:
                    print(f"  {sel}: {c} 个")

            # --- 样本产品链接 ---
            print("  --- 前 3 个产品链接样本 ---")
            sample_count = 0
            cards = cards1 if c1 > 0 else cards2
            for i in range(min(cards.count(), 10)):
                card = cards.nth(i)
                link_el = card.locator("a").first
                href = link_el.get_attribute("href") if link_el else None
                title_el = card.locator("h3").first
                title_text = title_el.text_content().strip() if title_el else "?"

                if href and href.startswith(prefix):
                    print(f"    {href}  — {title_text[:50]}")
                    sample_count += 1
                    if sample_count >= 3:
                        break

            # --- 品牌链接 ---
            import re

            brand_pattern = re.compile(rf"^/{re.escape(category)}/([a-z][a-z0-9-]+)/$")
            all_links = page.locator("a")
            brands = {}
            for i in range(min(all_links.count(), 500)):
                href = all_links.nth(i).get_attribute("href")
                text = all_links.nth(i).text_content().strip()
                if not href:
                    continue
                m = brand_pattern.match(href)
                if m:
                    slug = m.group(1)
                    if slug.isdigit():
                        continue
                    if slug not in brands:
                        brands[slug] = text if text != slug else "?"
            if brands:
                print(f"  品牌子页: {len(brands)} 个")
                for slug, name in sorted(brands.items())[:20]:
                    print(f"    {slug} — {name}")
                if len(brands) > 20:
                    print(f"    ... 还有 {len(brands) - 20} 个")

            # --- 翻页 ---
            next_btn = page.locator(".pagebar a.next")
            if next_btn.count() > 0:
                next_href = next_btn.first.get_attribute("href")
                print(f"  下一页: {next_href}")
            else:
                print("  下一页: 无")

        context.close()
        browser.close()
    print("\n全部探测完成!")
