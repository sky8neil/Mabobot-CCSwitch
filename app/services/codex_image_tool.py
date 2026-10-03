"""宿主图像生成工具：为 Codex 主线程提供第三方图像通道（dynamic tool）。

原生 image_gen 只在官方 ChatGPT 登录的 Codex Profile 下可用；本工具让
第三方 API Key（cc-switch 管理的通道）环境下的聊天也能真实生成图片。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.services.host_image_service import HostImageService, host_image_available

logger = logging.getLogger(__name__)

IMAGE_TOOL_NAMESPACE = "mabobot_image"
IMAGE_TOOL_GENERATE = "generate"

_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


class CodexImageToolError(RuntimeError):
    """宿主图像工具错误。"""


class CodexImageTool:
    """把宿主第三方图像通道暴露为 Codex dynamic tool。"""

    def __init__(self) -> None:
        self._service: Optional[HostImageService] = None

    @property
    def service(self) -> HostImageService:
        if self._service is None:
            self._service = HostImageService()
        return self._service

    def available(self) -> bool:
        return host_image_available() and self.service.available()

    def dynamic_tool_specs(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "namespace",
                "name": IMAGE_TOOL_NAMESPACE,
                "description": (
                    "Generate real images on the host and write them into this request's "
                    "output directory, returning saved paths. 宿主侧真实生图，图片直接写入本次请求输出目录。"
                ),
                "tools": [
                    {
                        "type": "function",
                        "name": IMAGE_TOOL_GENERATE,
                        "description": (
                            "Generate a real image on the host through the third-party "
                            "OpenAI-compatible image endpoint (宿主侧真实图像生成). It needs no "
                            "network shell; do NOT try to call the image API yourself with curl, "
                            "python, or any other code. The generated image is written into this "
                            "request's output directory, and the returned relative_path is the path "
                            "you must list for delivery. When the user directly asks for an image, "
                            "call this tool and then put the returned relative_path into the final "
                            "delivery protocol's `_mabobot_files` (unless you judge this image should "
                            "not be sent, for example it is only a document illustration). On failure "
                            "it returns `error`; do not falsely claim success and do not fall back to "
                            "drawing with code."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                    "description": "详细的中文/英文画面描述",
                                },
                                "count": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 4,
                                    "default": 1,
                                },
                            },
                            "required": ["prompt"],
                        },
                    }
                ],
            }
        ]

    def execute(
        self,
        *,
        namespace: Any,
        tool: Any,
        arguments: Any,
        context: Any,
        tracker: Any = None,
    ) -> Dict[str, Any]:
        if (
            str(namespace or "") != IMAGE_TOOL_NAMESPACE
            or str(tool or "") != IMAGE_TOOL_GENERATE
        ):
            raise CodexImageToolError("unsupported image tool call")

        params = arguments if isinstance(arguments, dict) else {}
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise CodexImageToolError("image prompt is empty")
        try:
            count = int(params.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(4, count))

        output_dir_raw = getattr(context, "output_dir", None)
        if not output_dir_raw:
            raise CodexImageToolError("image output directory is unavailable")
        output_dir = Path(output_dir_raw)
        if not output_dir.is_dir():
            raise CodexImageToolError("image output directory is unavailable")
        if getattr(context, "workspace_access", "") == "read_only":
            raise CodexImageToolError("chat workspace is read-only; image output is not allowed")

        try:
            results = self.service.generate(prompt=prompt, count=count)
        except Exception as exc:
            raise CodexImageToolError(f"image generation failed: {exc}") from exc

        if not results:
            raise CodexImageToolError("image generation failed: no image returned")

        try:
            from app.services.codex_browser_tool import _runtime_path
        except Exception:  # pragma: no cover - 独立导入时退化为宿主路径
            _runtime_path = None

        request_id = str(getattr(context, "request_id", "") or "")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        files: List[Dict[str, Any]] = []
        for index, item in enumerate(results, start=1):
            data, mime = item
            if not data:
                raise CodexImageToolError("image generation failed: empty image data")
            data = bytes(data)
            mime = str(mime or "image/png")
            ext = _MIME_EXTENSIONS.get(mime.lower(), ".png")
            filename = f"image_{stamp}-{index:02d}{ext}"
            path = output_dir / filename
            if path.exists():
                filename = f"image_{stamp}-{index:02d}-{uuid4().hex[:4]}{ext}"
                path = output_dir / filename
            path.write_bytes(data)

            runtime_path = str(path)
            if _runtime_path is not None:
                try:
                    runtime_path = _runtime_path(path, context)
                except Exception:
                    runtime_path = str(path)

            if tracker is not None and hasattr(tracker, "generated_image_paths"):
                resolved = str(path.resolve())
                if resolved not in tracker.generated_image_paths:
                    tracker.generated_image_paths.append(resolved)

            try:
                from app.services.codex_job_manager import codex_job_manager

                codex_job_manager.record_event(
                    request_id,
                    "host_image_generated",
                    {
                        "count": count,
                        "bytes": len(data),
                        "model": self.service.model,
                        "quality": self.service.quality,
                        "file_name": filename,
                    },
                )
            except Exception:
                logger.warning("记录 host_image_generated 事件失败", exc_info=True)

            logger.info(
                "宿主生图已保存: %s (%d 字节, model=%s, quality=%s)",
                filename,
                len(data),
                self.service.model,
                self.service.quality,
            )
            files.append(
                {
                    "saved_path": runtime_path,
                    "relative_path": filename,
                    "bytes": len(data),
                    "mime": mime,
                }
            )

        return {
            "namespace": IMAGE_TOOL_NAMESPACE,
            "tool": IMAGE_TOOL_GENERATE,
            "model": self.service.model,
            "quality": self.service.quality,
            "files": files,
            "count": len(files),
        }


_IMAGE_TOOL: Optional[CodexImageTool] = None
_IMAGE_TOOL_LOCK = threading.Lock()


def get_codex_image_tool() -> CodexImageTool:
    """返回线程安全的模块级单例。"""
    global _IMAGE_TOOL
    if _IMAGE_TOOL is None:
        with _IMAGE_TOOL_LOCK:
            if _IMAGE_TOOL is None:
                _IMAGE_TOOL = CodexImageTool()
    return _IMAGE_TOOL
