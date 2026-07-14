import json
from collections import Counter
from pathlib import Path

root = Path(__file__).parent.parent
raw_path = root / "data" / "products" / "raw"
file_paths = raw_path.glob("*.jsonl")

keys = []

for file in file_paths:
    with open(file, "r", encoding="utf-8") as f:
        for line in f:
            product = json.loads(line)

            if not isinstance(product, dict):
                continue

            for product_param in product:
                # 产品参数
                if product_param != "params":
                    keys.append(product_param)

                # 内部具体参数
                else:
                    params = product.get("params", {})

                    # 跳过异常产品
                    if isinstance(params, str) or not params:
                        continue

                    for category_param in params.values():
                        if not isinstance(category_param, dict):
                            continue

                        for param in category_param:
                            keys.append(param)

param_counter = Counter(keys)
