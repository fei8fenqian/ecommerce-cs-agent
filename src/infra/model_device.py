"""为本地检索模型选择可用的计算设备。"""

import logging

import torch

logger = logging.getLogger(__name__)


def resolve_model_device(configured_device: str) -> str:
    """根据配置和运行环境选择 CUDA 或 CPU。

    Args:
        configured_device: ``auto``、``cpu``、``cuda`` 或指定 CUDA 设备号。

    Returns:
        模型库可直接使用的设备字符串。CUDA 不可用时安全回退到 CPU。
    """
    requested = configured_device.strip().lower() or "auto"
    cuda_available = torch.cuda.is_available()
    if requested == "auto":
        return "cuda" if cuda_available else "cpu"
    if requested.startswith("cuda") and not cuda_available:
        logger.warning("CUDA requested for RAG models but unavailable; falling back to CPU")
        return "cpu"
    return requested
