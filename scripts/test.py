from pathlib import Path
root = Path(__file__).parent.parent
crawl_path = 'data/products/laptops_raw.jsonl'
print(f"{root}/{crawl_path}")