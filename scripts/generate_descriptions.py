def build_laptop_description(record: dict) -> str:
    """为每个产品生成对应自然语言描述"""
    sentences = []

    # 句1: 产品名称价格

    price_str = f"￥{record['price']}" if record.get("price") else "价格待定"
    name = record.get("product_name") or ""
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


def build_phone_description(p: dict) -> str:
    """为手机生成自然语言描述，用于向量检索"""
    sentences = []

    # 句1: 品牌 + 名称 + 价格
    price_str = f"￥{p['price']}" if p.get("price") else "价格待定"
    name = p.get("product_name") or ""
    brand = p.get("brand") or ""
    if name.startswith(brand):
        sentences.append(f"{name}，售价{price_str}")
    else:
        sentences.append(f"{brand}的{name}，售价{price_str}")

    # 句2: 上市时间 + 系统 + 定位
    meta = []
    if p.get("release_date"):
        meta.append(f"{p['release_date']}发布")
    if p.get("os"):
        meta.append(f"{p['os']}系统")
    if p.get("scene"):
        scene = p["scene"].replace(">", "").replace("，", "、")
        meta.append(scene)
    if meta:
        sentences.append("，".join(meta))

    # 句3: CPU + 内存 + 存储
    hw = []
    if p.get("cpu"):
        cpu = p["cpu"].replace(">", "").strip()
        hw.append(f"搭载{cpu}处理器")
    if p.get("ram"):
        hw.append(f"{p['ram']}内存")
    if p.get("storage"):
        hw.append(f"{p['storage']}存储")
    if hw:
        sentences.append("，".join(hw))

    # 句4: 屏幕
    screen = []
    if p.get("screen_size"):
        screen.append(f"{p['screen_size']}屏幕")
    if p.get("screen_type"):
        screen.append(p["screen_type"])
    if p.get("resolution"):
        screen.append(f"{p['resolution']}分辨率")
    if p.get("refresh_rate"):
        screen.append(f"{p['refresh_rate']}刷新率")
    if p.get("brightness"):
        screen.append(f"{p['brightness']}亮度")
    if p.get("screen_material"):
        screen.append(f"{p['screen_material']}材质")
    if screen:
        sentences.append("，".join(screen))

    # 句5: 摄像头
    camera = []
    if p.get("rear_camera_pixels"):
        camera.append(p["rear_camera_pixels"])
    if p.get("stabilization"):
        camera.append(p["stabilization"])
    if p.get("zoom"):
        camera.append(p["zoom"])
    if camera:
        sentences.append("摄像头：" + "，".join(camera))

    # 句6: 电池
    battery = []
    if p.get("battery_capacity"):
        battery.append(f"{p['battery_capacity']}电池")
    if p.get("wired_charging"):
        battery.append(f"{p['wired_charging']}有线充电")
    if p.get("wireless_charging"):
        battery.append(f"{p['wireless_charging']}无线充电")
    if battery:
        sentences.append("，".join(battery))

    # 句7: 机身
    body = []
    if p.get("weight"):
        body.append(f"重量{p['weight']}")
    if p.get("thickness"):
        body.append(f"厚度{p['thickness']}")
    if p.get("waterproof"):
        body.append(f"防水:{p['waterproof']}")
    if body:
        sentences.append("，".join(body))

    # 句8: 连接 + 功能
    conn_parts = []
    if p.get("network_type"):
        conn_parts.append(f"支持{p['network_type']}网络")
    if p.get("nfc") and "支持" in str(p.get("nfc", "")):
        conn_parts.append("支持NFC")
    if p.get("bluetooth"):
        conn_parts.append(p["bluetooth"])
    if p.get("fingerprint"):
        conn_parts.append(f"指纹:{p['fingerprint']}")
    if conn_parts:
        sentences.append("，".join(conn_parts))

    return "。".join(sentences) + "。"


def build_component_description(p: dict) -> str:
    """为配件生成自然语言描述，用于向量检索"""
    sentences = []

    price_str = f"￥{p['price']}" if p.get("price") else "价格待定"
    sentences.append(f"{p['name']}，售价{price_str}")

    cat = p["category"]
    norm = p.get("normalized", {})

    if cat == "cpu":
        parts = []
        if norm.get("socket"):
            parts.append(f"{norm['socket']}插槽")
        if norm.get("usage"):
            parts.append(norm["usage"])
        if norm.get("cores"):
            parts.append(f"{norm['cores']}")
        if norm.get("threads"):
            parts.append(f"共{norm['threads']}")
        if norm.get("tdp"):
            parts.append(f"TDP {norm['tdp']}")
        if norm.get("memory_type"):
            parts.append(f"支持{norm['memory_type']}内存")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "motherboard":
        parts = []
        if norm.get("socket"):
            parts.append(f"支持{norm['socket']}接口CPU")
        if norm.get("chipset"):
            parts.append(f"{norm['chipset']}芯片组")
        if norm.get("memory_type"):
            parts.append(f"支持{norm['memory_type']}内存")
        if norm.get("form_factor"):
            parts.append(f"{norm['form_factor']}板型")
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "vga":
        parts = []
        if norm.get("vram"):
            parts.append(f"{norm['vram']}显存")
        if norm.get("vram_type"):
            parts.append(f"{norm['vram_type']}类型")
        if norm.get("interface"):
            parts.append(norm["interface"])
        if norm.get("power_draw"):
            parts.append(f"功耗{norm['power_draw']}")
        if norm.get("gpu_type"):
            parts.append(norm["gpu_type"])
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "memory":
        parts = []
        if norm.get("memory_type"):
            parts.append(norm["memory_type"])
        if norm.get("capacity"):
            parts.append(norm["capacity"])
        if norm.get("frequency"):
            parts.append(f"频率{norm['frequency']}")
        if norm.get("usage"):
            parts.append(norm["usage"])
        if parts:
            sentences.append("，".join(parts))

    elif cat == "solid_state_drive":
        parts = []
        if norm.get("capacity"):
            parts.append(f"容量{norm['capacity']}")
        if norm.get("interface"):
            parts.append(norm["interface"])
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "hard_drives":
        parts = []
        if norm.get("capacity"):
            parts.append(f"容量{norm['capacity']}")
        if norm.get("interface"):
            parts.append(norm["interface"])
        if norm.get("rpm"):
            parts.append(f"转速{norm['rpm']}")
        if norm.get("form_factor"):
            parts.append(f"{norm['form_factor']}盘")
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "power":
        parts = []
        if norm.get("wattage"):
            parts.append(f"额定{norm['wattage']}")
        if norm.get("certification"):
            parts.append(f"{norm['certification']}认证")
        if norm.get("form_factor"):
            parts.append(norm["form_factor"])
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "case":
        parts = []
        if norm.get("case_type"):
            parts.append(norm["case_type"])
        if norm.get("motherboard_support"):
            parts.append(f"支持{norm['motherboard_support']}")
        if norm.get("gpu_length_max"):
            parts.append(f"显卡限长{norm['gpu_length_max']}")
        if norm.get("cooler_height_max"):
            parts.append(f"散热器限高{norm['cooler_height_max']}")
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    elif cat == "cooling_product":
        parts = []
        if norm.get("type"):
            parts.append(norm["type"])
        if norm.get("method"):
            parts.append(norm["method"])
        if norm.get("socket_support"):
            parts.append(f"兼容{norm['socket_support']}")
        if norm.get("dimensions"):
            parts.append(f"尺寸{norm['dimensions']}")
        if parts:
            sentences.append("，".join(parts))

    return "。".join(sentences) + "。"
