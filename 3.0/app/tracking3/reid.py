"""Local OSNet adapter; upstream architecture and MIT notice live in vendor/."""
import hashlib
import threading
from pathlib import Path

import numpy as np
from PIL import Image


class OSNetEncoder:
    def __init__(self, weights, device='cpu'):
        import torch
        from .vendor.osnet import osnet_x0_25
        if device not in ('cpu', 'mps'):
            raise ValueError('OSNet设备只支持cpu或mps')
        if device == 'mps' and not torch.backends.mps.is_available():
            raise ValueError('当前环境没有可用MPS，请显式改用cpu')
        self.torch, self.device = torch, device
        self.lock = threading.Lock()
        state = torch.load(weights, map_location='cpu', weights_only=True)
        if isinstance(state, dict) and 'state_dict' in state:
            state = state['state_dict']
        if not isinstance(state, dict):
            raise ValueError('OSNet权重必须为state_dict')
        state = {k.removeprefix('module.'): v for k, v in state.items()}
        classifier = state.get('classifier.weight')
        if classifier is None or classifier.ndim != 2 or classifier.shape[1] != 512:
            raise ValueError('权重与OSNet x0.25 / 512维协议不符')
        self.model = osnet_x0_25(num_classes=classifier.shape[0], pretrained=False)
        # No silently discarded layers or randomly initialized fallback features.
        self.model.load_state_dict(state, strict=True)
        self.model.eval().to(device)
        weights_hash = hashlib.sha256(Path(weights).read_bytes()).hexdigest()
        architecture_hash = hashlib.sha256(Path(__file__).with_name('vendor').joinpath('osnet.py').read_bytes()).hexdigest()
        self.version = f'osnet-x0.25:rgb-bilinear-imagenet-256x128-v1:{architecture_hash[:16]}:{weights_hash}'

    def extract(self, rgb):
        if rgb.ndim != 3 or rgb.shape[2] != 3 or not rgb.size:
            raise ValueError('目标图像必须为非空RGB图像')
        image = np.asarray(Image.fromarray(rgb).resize((128, 256), Image.Resampling.BILINEAR), dtype=np.float32) / 255
        image = (image - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
        tensor = self.torch.from_numpy(image.transpose(2, 0, 1).copy()).unsqueeze(0).to(self.device)
        with self.lock, self.torch.inference_mode():
            vector = self.model(tensor).cpu().numpy()[0]
        if vector.shape != (512,) or not np.isfinite(vector).all():
            raise ValueError('OSNet未返回有效512维特征')
        norm = np.linalg.norm(vector)
        if norm <= 0:
            raise ValueError('OSNet返回零特征')
        return (vector / norm).astype(float).tolist()
