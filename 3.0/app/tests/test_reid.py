import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
import torch
import torchvision.transforms as T

from tracking3.engine import Encoder


WEIGHTS = Path(__file__).resolve().parents[1] / 'models/osnet_x0_25_msmt17.pth'


@unittest.skipUnless(WEIGHTS.is_file(), '本地专用权重未下载')
class ReIDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(4)
        cls.encoder = Encoder(osnet=WEIGHTS)

    def test_matches_upstream_feature_extractor_protocol(self):
        # Independent Torchvision pipeline, as specified by upstream FeatureExtractor.
        rgb = np.random.default_rng(41).integers(0, 256, (71, 39, 3), dtype=np.uint8)
        transform = T.Compose([T.Resize((256, 128)), T.ToTensor(),
                               T.Normalize([.485, .456, .406], [.229, .224, .225])])
        with torch.inference_mode():
            vector = self.encoder.reid.model(transform(Image.fromarray(rgb)).unsqueeze(0)).numpy()[0]
        vector /= np.linalg.norm(vector)
        np.testing.assert_allclose(self.encoder.extract(rgb), vector, atol=2e-6)

    def test_deterministic_and_rejects_invalid_input(self):
        rgb = np.full((128, 64, 3), 90, dtype=np.uint8)
        a, b = self.encoder.extract(rgb), self.encoder.extract(rgb)
        self.assertEqual(len(a), 512)
        np.testing.assert_allclose(a, b, atol=1e-7)
        self.assertAlmostEqual(np.linalg.norm(a), 1, places=6)
        with self.assertRaises(ValueError):
            self.encoder.extract(np.empty((0, 64, 3), dtype=np.uint8))

    def test_incomplete_backbone_is_not_silently_accepted(self):
        state = torch.load(WEIGHTS, map_location='cpu', weights_only=True)
        del state['conv1.conv.weight']
        with tempfile.TemporaryDirectory() as root:
            broken = Path(root) / 'broken.pth'
            torch.save(state, broken)
            with self.assertRaises(RuntimeError):
                Encoder(osnet=broken)


if __name__ == '__main__':
    unittest.main()
