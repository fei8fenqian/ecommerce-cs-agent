"""scripts/crawl/crawl_troubleshooting.py — 爬取官方支持页面，刷新故障排查知识库。

用法:
    python scripts/crawl/crawl_troubleshooting.py              # 爬全部
    python scripts/crawl/crawl_troubleshooting.py --brand apple  # 只爬 Apple
    python scripts/crawl/crawl_troubleshooting.py --brand huawei --dry-run  # 预览不保存

支持的源:
    Apple   — support.apple.com/zh-cn
    华为    — consumer.huawei.com/cn/support
    联想    — iknow.lenovo.com.cn
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent.parent.parent
OUTPUT = ROOT / "data" / "raw"


# =============================================================================
# 数据源配置
# =============================================================================
SOURCES = {
    "apple": [
        {
            "name": "mac_startup",
            "url": "https://support.apple.com/zh-cn/102623",
            "selectors": ["article", "#content", ".main-content"],
            "labels": ["Mac 无法开机"],
        },
        {
            "name": "mac_power_troubleshooting",
            "url": "https://support.apple.com/zh-cn/117313",
            "selectors": ["article", "#content", ".main-content"],
            "labels": ["Mac 电源和启动问题"],
        },
        {
            "name": "iphone_recovery",
            "url": "https://support.apple.com/zh-cn/guide/iphone/iph8903c3ee6/ios",
            "selectors": ["article", "#content", ".main-content"],
            "labels": ["iPhone 恢复模式"],
        },
    ],
    "huawei": [
        {
            "name": "matebook_no_power",
            "url": "https://consumer.huawei.com/cn/support/content/zh-cn00803483/",
            "selectors": ["article", ".support-content", ".content-main"],
            "labels": ["MateBook 无法开机", "笔记本 开机 故障"],
        },
        {
            "name": "matebook_stuck_logo",
            "url": "https://consumer.huawei.com/cn/support/content/zh-cn00691908/",
            "selectors": ["article", ".support-content", ".content-main"],
            "labels": ["MateBook 卡 Logo", "笔记本 卡住 开机"],
        },
        {
            "name": "matebook_black_screen_list",
            "url": "https://consumer.huawei.com/cn/support/content/knowledgelist/zh-cn-vol15939780/",
            "selectors": ["article", ".support-content", ".content-main"],
            "labels": ["MateBook 黑屏合集"],
        },
    ],
    "lenovo": [
        {
            "name": "thinkpad_error_codes",
            "url": "https://iknow.lenovo.com.cn/detail/021225",
            "selectors": [".detail-content", ".iknow-content", "article"],
            "labels": ["ThinkPad 报错码", "POST 错误"],
        },
        {
            "name": "lenovo_startup_trouble",
            "url": "https://iknow.lenovo.com.cn/detail/021353",
            "selectors": [".detail-content", ".iknow-content", "article"],
            "labels": ["联想 启动故障"],
        },
        {
            "name": "lenovo_black_screen",
            "url": "https://newsupport.lenovo.com.cn/commonProblemsDetail.html?noteid=114230",
            "selectors": [".detail-content", ".iknow-content", "article"],
            "labels": ["联想 黑屏"],
        },
    ],
}


# =============================================================================
# 爬取逻辑
# =============================================================================
async def crawl_page(page, source: dict) -> dict:
    """爬单个页面，返回 {name, url, title, text, html, crawled_at}"""
    url = source["url"]
    selectors = source["selectors"]

    await page.goto(url, wait_until="networkidle", timeout=30_000)

    # 拿标题
    title_el = await page.locator("h1").first
    title = (await title_el.text_content() or "").strip() if await title_el.count() > 0 else ""

    # 拿正文 — 按选择器兜底
    content_locator = None
    for sel in selectors:
        loc = page.locator(sel)
        if await loc.count() > 0:
            content_locator = loc.first
            break

    if content_locator is None:
        content_locator = page.locator("body")

    html = await content_locator.inner_html()
    text = await content_locator.text_content() or ""

    # 基础清洗
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    clean_text = "\n".join(lines)

    return {
        "name": source["name"],
        "url": url,
        "title": title,
        "text": clean_text,
        "html": html,
        "labels": source.get("labels", []),
        "crawled_at": datetime.now().isoformat(),
    }


async def crawl_brand(brand: str, dry_run: bool = False):
    """爬某个品牌的所有页面"""
    sources = SOURCES.get(brand, [])
    if not sources:
        print(f"未知品牌: {brand}，可选: {list(SOURCES.keys())}")
        return []

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for src in sources:
            print(f"  爬取: {src['name']} ({src['url']})")
            try:
                result = await crawl_page(page, src)
                results.append(result)
                print(f"    ✓ {len(result['text'])} 字符, 标题: {result['title'][:50]}")
            except Exception as e:
                print(f"    ✗ 失败: {e}")

        await browser.close()

    # 保存
    if not dry_run and results:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        out_file = OUTPUT / f"troubleshooting_{brand}_{datetime.now():%Y%m%d_%H%M%S}.json"
        out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已保存: {out_file}")
    elif dry_run:
        for r in results:
            print(f"\n--- {r['name']} ({len(r['text'])} chars) ---")
            print(r["text"][:300])

    return results


# =============================================================================
# 主入口
# =============================================================================
async def main():
    parser = argparse.ArgumentParser(description="爬取 3C 故障排查支持页面")
    parser.add_argument("--brand", choices=list(SOURCES.keys()), help="只爬某个品牌")
    parser.add_argument("--dry-run", action="store_true", help="预览不保存")
    args = parser.parse_args()

    brands = [args.brand] if args.brand else list(SOURCES.keys())

    print(f"开始爬取 {len(brands)} 个品牌: {brands}")
    total = 0
    for brand in brands:
        print(f"\n[{brand}]")
        results = await crawl_brand(brand, dry_run=args.dry_run)
        total += len(results)

    print(f"\n完成。共爬取 {total} 个页面。")


if __name__ == "__main__":
    asyncio.run(main())
