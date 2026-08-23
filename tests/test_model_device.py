"""本地检索模型设备选择测试。"""

from unittest.mock import patch

from infra.model_device import resolve_model_device


def test_auto_uses_cuda_when_available():
    """开发机存在 NVIDIA GPU 时，自动使用 CUDA。"""
    with patch("infra.model_device.torch.cuda.is_available", return_value=True):
        assert resolve_model_device("auto") == "cuda"


def test_auto_uses_cpu_when_cuda_is_unavailable():
    """云端 CPU 部署保持可用，不因 GPU 配置缺失启动失败。"""
    with patch("infra.model_device.torch.cuda.is_available", return_value=False):
        assert resolve_model_device("auto") == "cpu"
        assert resolve_model_device("cuda:0") == "cpu"


def test_explicit_cpu_never_needs_cuda():
    """需要排障或 CPU 基准测试时可显式关闭 GPU。"""
    with patch("infra.model_device.torch.cuda.is_available", return_value=True):
        assert resolve_model_device("cpu") == "cpu"
