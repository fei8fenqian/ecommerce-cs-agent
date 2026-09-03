"""面向 Web 商品目录的只读查询，不承担成交、库存扣减或价格快照职责。"""

import re
import unicodedata
from typing import Any, Literal, Mapping

from infra.db_pool import get_connection, put_connection
from infra.product_image_catalog import product_image_urls

ProductCategory = Literal["laptops", "phones", "components"]

_PRODUCT_TABLES: dict[ProductCategory, str] = {
    "laptops": "laptop_products",
    "phones": "phone_products",
    "components": "component_products",
}

_COMPONENT_CATEGORY_NAMES = {
    "cpu": "CPU",
    "vga": "显卡",
    "motherboard": "主板",
    "memory": "内存",
    "solid_state_drive": "固态硬盘",
    "hard_drives": "机械硬盘",
    "power": "电源",
    "case": "机箱",
    "cooling_product": "散热器",
}

_SPEC_LABELS = {
    "cpu": "处理器",
    "cpu_series": "处理器系列",
    "cpu_cores": "核心 / 线程",
    "cpu_turbo_freq": "最高睿频",
    "ram": "内存",
    "ram_type": "内存类型",
    "storage": "存储容量",
    "storage_desc": "硬盘说明",
    "screen_size": "屏幕尺寸",
    "resolution": "屏幕分辨率",
    "refresh_rate": "刷新率",
    "brightness": "亮度",
    "gpu_type": "显卡类型",
    "gpu_chip": "显卡",
    "gpu_vram": "显存",
    "battery": "电池",
    "battery_capacity": "电池容量",
    "wired_charging": "有线充电",
    "wireless_charging": "无线充电",
    "camera_total": "摄像头",
    "rear_camera_pixels": "后置主摄",
    "screen_type": "屏幕类型",
    "screen_material": "屏幕材质",
    "network_type": "网络类型",
    "nfc": "NFC",
    "os": "操作系统",
    "socket": "CPU 插槽",
    "memory_type": "内存规格",
    "chipset": "芯片组",
    "form_factor": "规格 / 板型",
    "capacity": "容量",
    "interface": "接口",
    "wattage": "额定功率",
    "vram": "显存容量",
}

_DETAIL_IGNORED_FIELDS = {"id", "url", "text", "source_url", "name", "brand", "price", "product_name", "category"}


def _catalog_query_tokens(query: str) -> list[str]:
    """Normalize customer catalog input into bounded, duplicate-free tokens."""
    normalized = unicodedata.normalize("NFKC", query).casefold().strip()
    tokens = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized)
    return list(dict.fromkeys(token for token in tokens if token))[:8]


def _catalog_search_clause(tokens: list[str], *, brand_column: str = "brand") -> tuple[str, tuple[str, ...]]:
    """Build AND token matching over whitespace-insensitive product/brand text."""
    if not tokens:
        return "", ()
    normalized_name = "regexp_replace(lower(product_name), '\\s+', '', 'g')"
    normalized_brand = f"regexp_replace(lower({brand_column}), '\\s+', '', 'g')"
    clauses = [f"({normalized_name} LIKE %s OR {normalized_brand} LIKE %s)" for _ in tokens]
    params = tuple(value for token in tokens for value in (f"%{token}%", f"%{token}%"))
    return " AND " + " AND ".join(clauses), params


def _image_url(product_id: str, metadata: Any) -> str | None:
    """优先使用元数据图片；旧数据则从源数据索引补齐公开缩略图。"""
    if not isinstance(metadata, dict):
        return product_image_urls().get(product_id)
    value = metadata.get("image_url") or metadata.get("thumbnail_url")
    return str(value) if value else product_image_urls().get(product_id)


def _public_specifications(metadata: Any, category: ProductCategory) -> list[dict[str, str]]:
    """将历史商品元数据收敛为详情页可展示的短规格表。"""
    if not isinstance(metadata, dict):
        return []
    source = metadata.get("normalized", {}) if category == "components" else metadata
    if not isinstance(source, dict):
        return []
    specifications: list[dict[str, str]] = []
    for key, value in source.items():
        if key in _DETAIL_IGNORED_FIELDS or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text:
            specifications.append({"name": _SPEC_LABELS.get(str(key), str(key).replace("_", " ")), "value": text})
    return specifications[:24]


async def list_products(
    category: ProductCategory,
    query: str = "",
    limit: int = 24,
) -> list[dict[str, Any]]:
    """返回浏览器可展示的最小商品目录字段。

    Args:
        category: 受白名单限制的商品类别，决定固定数据表。
        query: 按商品名或品牌进行的可选模糊筛选。
        limit: 受 API 上层限制的返回数量。

    Returns:
        商品目录行；不包含 embedding、完整内部元数据或任何成交事实。
    """
    table = _PRODUCT_TABLES[category]
    # 两张历史商品表的公共字段相同，但 phone_products 没有 product_type。
    # 目录展示层使用稳定别名，而不是为了一个展示字段改动历史入库表。
    product_type_column = "product_type" if category == "laptops" else "'手机'"
    tokens = _catalog_query_tokens(query)
    search_sql, search_params = _catalog_search_clause(tokens)
    sql = f"""
        SELECT id, product_name, brand, price, description, {product_type_column} AS product_type,
               status, stock, warehouse, metadata
        FROM {table}
        WHERE TRUE {search_sql}
        ORDER BY product_name ASC
        LIMIT %s
    """
    connection = await get_connection()
    try:
        cursor = await connection.execute(sql, (*search_params, limit))
        products: list[dict[str, Any]] = []
        async for row in cursor:
            (
                product_id,
                product_name,
                brand,
                price,
                description,
                product_type,
                status,
                stock,
                warehouse,
                metadata,
            ) = row
            products.append(
                {
                    "id": str(product_id),
                    "product_name": str(product_name or "未命名商品"),
                    "brand": str(brand or ""),
                    "price": float(price) if price is not None else None,
                    "description": str(description or ""),
                    "product_type": str(product_type or ""),
                    "status": str(status or ""),
                    "stock": int(stock or 0),
                    "warehouse": str(warehouse or ""),
                    "image_url": _image_url(str(product_id), metadata),
                }
            )
        return products
    finally:
        await put_connection(connection)


async def list_product_page(
    category: ProductCategory,
    query: str = "",
    brand: str = "",
    component_category: str = "",
    page: int = 1,
    page_size: int = 24,
) -> dict[str, Any]:
    """按页返回可公开浏览的商品，并同时提供当前类别的品牌筛选项。

    Args:
        category: 固定的目录分类；只会映射到白名单中的历史产品表。
        query: 商品名或品牌关键词。
        brand: 已选品牌。配件没有可靠品牌字段时忽略此筛选。
        component_category: 配件二级分类，如 ``cpu`` 或 ``vga``。
        page: 从 1 开始的页码。
        page_size: 每页条数；由 API 层限制在适合目录展示的范围。

    Returns:
        包含商品、总数、品牌和分页信息的只读目录页。
    """
    table = _PRODUCT_TABLES[category]
    offset = (page - 1) * page_size
    tokens = _catalog_query_tokens(query)
    normalized_brand = brand.strip()
    normalized_component_category = component_category.strip()

    if category == "components":
        product_type_column = "category"
        brand_column = "COALESCE(metadata->>'brand', '')"
        status_column = "'在售'"
        component_clause = "AND (%s = '' OR category = %s)"
        component_params: tuple[str, ...] = (normalized_component_category, normalized_component_category)
    else:
        product_type_column = "product_type" if category == "laptops" else "'手机'"
        brand_column = "brand"
        status_column = "status"
        component_clause = ""
        component_params = ()

    search_sql, search_params = _catalog_search_clause(tokens, brand_column=brand_column)
    where_sql = f"""
        WHERE TRUE {search_sql}
          AND (%s = '' OR {brand_column} = %s)
          {component_clause}
    """
    filter_params: tuple[str, ...] = (
        *search_params,
        normalized_brand,
        normalized_brand,
        *component_params,
    )
    connection = await get_connection()
    try:
        total_cursor = await connection.execute(
            f"SELECT COUNT(*) FROM {table} {where_sql}",
            filter_params,
        )
        total_row = await total_cursor.fetchone()
        total = int(total_row[0]) if total_row else 0
        cursor = await connection.execute(
            f"""
            SELECT id, product_name, {brand_column} AS brand, price, description,
                   {product_type_column} AS product_type, {status_column} AS status,
                   COALESCE(stock, 0) AS stock, COALESCE(warehouse, '') AS warehouse, metadata
            FROM {table}
            {where_sql}
            ORDER BY product_name ASC
            LIMIT %s OFFSET %s
            """,
            (*filter_params, page_size, offset),
        )
        products: list[dict[str, Any]] = []
        async for row in cursor:
            (
                product_id,
                product_name,
                product_brand,
                price,
                description,
                product_type,
                status,
                stock,
                warehouse,
                metadata,
            ) = row
            product_type_text = str(product_type or "")
            if category == "components":
                product_type_text = _COMPONENT_CATEGORY_NAMES.get(product_type_text, product_type_text)
            products.append(
                {
                    "id": str(product_id),
                    "product_name": str(product_name or "未命名商品"),
                    "brand": str(product_brand or ""),
                    "price": float(price) if price is not None else None,
                    "description": str(description or ""),
                    "product_type": product_type_text,
                    "status": str(status or ""),
                    "stock": int(stock or 0),
                    "warehouse": str(warehouse or ""),
                    "image_url": _image_url(str(product_id), metadata),
                }
            )

        brands_cursor = await connection.execute(
            f"SELECT DISTINCT {brand_column} AS brand FROM {table} WHERE {brand_column} <> '' ORDER BY brand ASC"
        )
        brands = [str(row[0]) async for row in brands_cursor if row[0]]
        return {
            "products": products,
            "total": total,
            "page": page,
            "page_size": page_size,
            "brands": brands,
            "component_categories": _COMPONENT_CATEGORY_NAMES if category == "components" else {},
        }
    finally:
        await put_connection(connection)


async def get_product_detail(category: ProductCategory, product_id: str) -> dict[str, Any] | None:
    """读取单个商品的公开详情，供客户在目录页展开规格与咨询入口。"""
    table = _PRODUCT_TABLES[category]
    if category == "components":
        product_type_column = "category"
        brand_column = "COALESCE(metadata->>'brand', '')"
        status_column = "'在售'"
    else:
        product_type_column = "product_type" if category == "laptops" else "'手机'"
        brand_column = "brand"
        status_column = "status"
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT id, product_name, {brand_column} AS brand, price, description,
                   {product_type_column} AS product_type, {status_column} AS status,
                   COALESCE(stock, 0) AS stock, COALESCE(warehouse, '') AS warehouse, metadata
            FROM {table}
            WHERE id = %s
            """,
            (product_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        product_type = str(row[5] or "")
        if category == "components":
            product_type = _COMPONENT_CATEGORY_NAMES.get(product_type, product_type)
        product = {
            "id": str(row[0]),
            "product_name": str(row[1] or "未命名商品"),
            "brand": str(row[2] or ""),
            "price": float(row[3]) if row[3] is not None else None,
            "description": str(row[4] or ""),
            "product_type": product_type,
            "status": str(row[6] or ""),
            "stock": int(row[7] or 0),
            "warehouse": str(row[8] or ""),
            "image_url": _image_url(str(row[0]), row[9]),
            "specifications": _public_specifications(row[9], category),
        }
        return product
    finally:
        await put_connection(connection)


def build_public_product_context(product: Mapping[str, Any]) -> str:
    """把详情页已展示的商品事实转换为模型可引用的受控上下文。

    该内容来自当前商品 ID 的数据库读取，而不是用户粘贴的长标题或向量检索猜测；
    不包含仓库、精确库存或内部字段。
    """
    price = product.get("price")
    price_text = f"¥{price}" if isinstance(price, (int, float)) else "价格待询"
    lines = [
        "当前商品详情（优先依据）：",
        f"名称：{str(product.get('product_name') or '')}",
        f"品牌：{str(product.get('brand') or '')}",
        f"类别：{str(product.get('product_type') or '')}",
        f"公开价格：{price_text}",
        f"简介：{str(product.get('description') or '')[:700]}",
    ]
    specifications = product.get("specifications")
    if isinstance(specifications, list):
        for specification in specifications[:16]:
            if not isinstance(specification, Mapping):
                continue
            name = str(specification.get("name") or "").strip()
            value = str(specification.get("value") or "").strip()
            if name and value:
                lines.append(f"{name}：{value}")
    return "\n".join(lines)
