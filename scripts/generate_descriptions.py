def build_description(record: dict) -> str:
    """为每个产品生成对应自然语言描述"""
    sentences = []

    # 句1: 产品名称价格

    price_str = f"￥{record['price']}" if record.get("price") else "价格待定"
    name = record.get("product_name")
    if name.startswith(record["brand"]):
        sentences.append(f"{name}，售价{price_str}")
    else:
        sentences.append(f"{record['brand']}的{name}，售价{price_str}")

    # 句2: 产品定位
    meta = []
    if record.get("product_type"):
        meta.append(record["product_type"])
    if record.get("release_date"):
        meta.append(f"{record['release_date']}发布")
    if record.get("os"):
        meta.append(f"预装{record['os']}")
    if meta:
        sentences.append("，".join(meta))
    # 句3: CPU
    cpu_parts = []
    if record.get("cpu"):
        cpu_parts.append(f"搭载{record['cpu']}处理器")
    if record.get("cpu_cores"):
        cpu_parts.append(f"{record['cpu_cores']}")
    if record.get("cpu_turbo_freq"):
        cpu_parts.append(f"最高睿频{record['cpu_turbo_freq']}")
    if cpu_parts:
        sentences.append("，".join(cpu_parts))

    # 句4: 内存+存储
    mem = []
    if record.get("ram"):
        ram_str = f"{record['ram']}"
        if record.get("ram_type"):
            ram_str += f" {record['ram_type']}"
        mem.append(f"配备{ram_str}内存")
    if record.get("storage"):
        storage_str = record["storage"]
        if record.get("storage_desc"):
            storage_str += f" {record['storage_desc']}"
        mem.append(storage_str)
    if mem:
        sentences.append("，".join(mem))

    # 句5: 屏幕
    screen = []
    if record.get("screen_size"):
        size_str = record["screen_size"]
        if record.get("screen_ratio"):
            size_str += f" {record['screen_ratio']}"
        screen.append(size_str)
    elif record.get("screen_ratio"):
        screen.append(record["screen_ratio"])

    if screen:
        screen[0] += "屏幕"

    if record.get("resolution"):
        screen.append(f"{record['resolution']}分辨率")
    if record.get("refresh_rate"):
        screen.append(f"{record['refresh_rate']}刷新率")
    if record.get("brightness"):
        screen.append(f"{record['brightness']}亮度")
    if record.get("color_gamut"):
        screen.append(f"{record['color_gamut']}色域")
    if screen:
        sentences.append("，".join(screen))

    # 句6: GPU
    gpu = []
    if record.get("gpu_type"):
        gpu.append(record["gpu_type"])
    if record.get("gpu_chip"):
        gpu.append(record["gpu_chip"])
    if record.get("gpu_vram"):
        gpu.append(f"{record['gpu_vram']}显存")
    if gpu:
        sentences.append("，".join(gpu))

    # 句7: 接口
    io = []
    if record.get("usb_ports"):
        io.append(f"USB接口：{record['usb_ports']}")
    if record.get("video_ports"):
        io.append(f"视频接口：{record['video_ports']}")
    if io:
        sentences.append("；".join(io))

    # 句8: 电池+机身
    body = []
    if record.get("battery"):
        body.append(record["battery"])
    if record.get("weight"):
        body.append(f"重量{record['weight']}")
    if record.get("thickness"):
        body.append(f"厚度{record['thickness']}")
    if body:
        sentences.append("，".join(body))

    # 句9: 续航（极稀疏，单独判断）
    if record.get("battery_life"):
        sentences.append(f"续航{record['battery_life']}")

    return "。".join(sentences) + "。"
