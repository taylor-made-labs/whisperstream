"""Intel Mac storage owner for the shared CPU ONNX model inventory.

The existing ONNX lifecycle implementation performs filesystem operations only;
reuse its serialization and validation rather than acquiring Apple Silicon
Core ML models that Intel's speech worker cannot load.
"""

from .model_lifecycle_contract import ModelAssetRef
from .windows_model_lifecycle import WindowsModelLifecycleAdapter


class MacosOnnxModelLifecycleAdapter(WindowsModelLifecycleAdapter):
    @staticmethod
    def _location_token(asset: ModelAssetRef) -> str:
        token = WindowsModelLifecycleAdapter._location_token(asset)
        return token.replace("windows-model-", "macos-onnx-model-", 1)
