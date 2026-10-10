"""Image generation providers: OpenAI Images API + ComfyUI local."""

import asyncio
import base64
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict

import httpx

logger = logging.getLogger("tavern.image")

_IMG_CACHE_MAX = 20


class ImageProvider(ABC):
    @abstractmethod
    async def generate_image(self, prompt: str, style: str = "cinematic") -> dict:
        """Generate an image from prompt. Returns {"base64": str, "provider": str}."""


class OpenAIImageProvider(ImageProvider):
    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-image-1")
        self.size = config.get("size", "1536x1024")
        self.quality = config.get("quality", "low")
        self.base_url = config.get("base_url")
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            kwargs["timeout"] = httpx.Timeout(120.0, connect=10.0)
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def generate_image(self, prompt: str, style: str = "cinematic") -> dict:
        logger.info("OpenAI image gen: model=%s, size=%s, prompt=%s",
                    self.model, self.size, prompt[:80])
        if self.base_url:
            return await self._generate_via_httpx(prompt)
        client = self._get_client()
        response = await client.images.generate(
            model=self.model,
            prompt=prompt,
            n=1,
            size=self.size,
            quality=self.quality,
            response_format="b64_json",
        )
        img_data = response.data[0].b64_json
        return {"base64": img_data, "provider": "openai"}

    async def _generate_via_httpx(self, prompt: str) -> dict:
        """Direct HTTP request for non-standard OpenAI-compatible endpoints."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        body = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": self.size,
            "quality": self.quality,
            "response_format": "b64_json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = await client.post(self.base_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        logger.debug("Image API response keys: %s", list(data.keys()))
        # OpenAI-style: {"data": [{"b64_json": "..."}]}
        if "data" in data and data["data"]:
            item = data["data"][0]
            if item.get("b64_json"):
                return {"base64": item["b64_json"], "provider": "openai"}
            if item.get("url"):
                img_bytes = await self._download_image(item["url"])
                return {"base64": base64.b64encode(img_bytes).decode(), "provider": "openai"}
        # Fallback: {"images": [...]}
        if "images" in data and data["images"]:
            item = data["images"][0]
            if isinstance(item, str):
                if item.startswith("http"):
                    img_bytes = await self._download_image(item)
                    return {"base64": base64.b64encode(img_bytes).decode(), "provider": "openai"}
                return {"base64": item, "provider": "openai"}
            if isinstance(item, dict):
                url = item.get("url") or item.get("image_url")
                if url:
                    img_bytes = await self._download_image(url)
                    return {"base64": base64.b64encode(img_bytes).decode(), "provider": "openai"}
                if item.get("b64_json") or item.get("base64"):
                    return {"base64": item.get("b64_json") or item.get("base64"), "provider": "openai"}
        raise ValueError(f"Unexpected image API response format: {list(data.keys())}")

    @staticmethod
    async def _download_image(url: str) -> bytes:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content


class ComfyUIImageProvider(ImageProvider):
    def __init__(self, config: dict):
        self.base_url = config.get("base_url", "http://127.0.0.1:8188")
        self.width = config.get("width", 1024)
        self.height = config.get("height", 576)
        self.steps = config.get("steps", 20)
        self.checkpoint = config.get("checkpoint", "")
        self.timeout = config.get("timeout", 120)

    def _build_workflow(self, prompt: str) -> dict:
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": int(time.time()) % (2**32),
                    "steps": self.steps,
                    "cfg": 7.0,
                    "sampler_name": "euler_ancestral",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                }
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {
                    "ckpt_name": self.checkpoint or "sd_xl_base_1.0.safetensors"
                }
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {
                    "width": self.width,
                    "height": self.height,
                    "batch_size": 1
                }
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": prompt,
                    "clip": ["4", 1]
                }
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": "blurry, low quality, text, watermark, distorted",
                    "clip": ["4", 1]
                }
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["3", 0],
                    "vae": ["4", 2]
                }
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {
                    "filename_prefix": "tavern_scene",
                    "images": ["8", 0]
                }
            }
        }

    async def generate_image(self, prompt: str, style: str = "cinematic") -> dict:
        workflow = self._build_workflow(prompt)
        payload = {"prompt": workflow}

        logger.info("ComfyUI image gen: %s, %dx%d, steps=%d",
                    self.base_url, self.width, self.height, self.steps)

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
            resp = await client.post(f"{self.base_url}/prompt", json=payload)
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

            # Poll for completion
            deadline = time.time() + self.timeout
            filename = None
            subfolder = ""
            while time.time() < deadline:
                await asyncio.sleep(1.5)
                hist_resp = await client.get(f"{self.base_url}/history/{prompt_id}")
                if hist_resp.status_code != 200:
                    continue
                hist = hist_resp.json()
                if prompt_id in hist:
                    outputs = hist[prompt_id].get("outputs", {})
                    for node_out in outputs.values():
                        images = node_out.get("images", [])
                        if images:
                            filename = images[0]["filename"]
                            subfolder = images[0].get("subfolder", "")
                            break
                    if filename:
                        break

            if not filename:
                raise TimeoutError("ComfyUI image generation timed out")

            # Fetch the image
            params = {"filename": filename}
            if subfolder:
                params["subfolder"] = subfolder
            img_resp = await client.get(f"{self.base_url}/view", params=params)
            img_resp.raise_for_status()
            img_b64 = base64.b64encode(img_resp.content).decode("ascii")

        return {"base64": img_b64, "provider": "comfyui"}


class CachedImageProvider(ImageProvider):
    """Wraps any ImageProvider with a simple LRU cache keyed on prompt hash."""

    def __init__(self, inner: ImageProvider, max_size: int = _IMG_CACHE_MAX):
        self._inner = inner
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._max = max_size

    async def generate_image(self, prompt: str, style: str = "cinematic") -> dict:
        key = hashlib.md5(f"{prompt}|{style}".encode()).hexdigest()[:16]
        if key in self._cache:
            self._cache.move_to_end(key)
            logger.debug("Image cache hit: %s", key)
            return self._cache[key]

        result = await self._inner.generate_image(prompt, style)
        self._cache[key] = result
        if len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return result


class GeminiImageProvider(ImageProvider):
    """Gemini-style image generation API (JD Cloud, Google Gemini, etc)."""

    def __init__(self, config: dict):
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", "")
        self.model = config.get("model", "Gemini 3-Pro-Image-Preview")

    async def generate_image(self, prompt: str, style: str = "cinematic") -> dict:
        import uuid
        logger.info("Gemini image gen: model=%s, prompt=%s", self.model, prompt[:80])
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "Trace-Id": str(uuid.uuid4()),
        }
        body = {
            "model": self.model,
            "contents": {
                "role": "user",
                "parts": {"text": prompt},
            },
            "generation_config": {
                "response_modalities": ["IMAGE"],
            },
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = await client.post(self.base_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        logger.debug("Gemini image response keys: %s", list(data.keys()))
        if "candidates" in data and data["candidates"]:
            parts = (data["candidates"][0].get("content") or {}).get("parts", [])
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and isinstance(inline, dict) and inline.get("data"):
                    return {"base64": inline["data"], "provider": "gemini"}
        raise ValueError(f"Gemini image API: no image in response. Keys: {list(data.keys())}")


def create_image_provider(settings: dict) -> ImageProvider | None:
    """Factory: create provider from image_settings config dict."""
    if not settings or not settings.get("enabled"):
        return None
    provider_type = settings.get("provider", "disabled")
    if provider_type == "disabled":
        return None

    if provider_type == "openai":
        cfg = settings.get("openai", {})
        if not cfg.get("api_key"):
            return None
        inner = OpenAIImageProvider(cfg)
    elif provider_type == "gemini":
        cfg = settings.get("gemini", {})
        if not cfg.get("api_key"):
            return None
        if not cfg.get("base_url"):
            return None
        inner = GeminiImageProvider(cfg)
    elif provider_type == "comfyui":
        cfg = settings.get("comfyui", {})
        inner = ComfyUIImageProvider(cfg)
    else:
        return None

    return CachedImageProvider(inner)
