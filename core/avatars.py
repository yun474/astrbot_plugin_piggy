"""Bounded, in-memory QQ avatar cache. No third-party profile lookup."""

import asyncio
import io
import time
from collections import OrderedDict
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageOps


class Avatars:
    def __init__(self):
        self.cache = OrderedDict()
        self.session = None
        self.limit = asyncio.Semaphore(4)

    async def get_many(self, app_id: str, players: list[dict]) -> dict[str, bytes]:
        ids = list(dict.fromkeys(p["open_id"] for p in players))
        values = await asyncio.gather(*(self.get(app_id, open_id) for open_id in ids))
        return {key: value for key, value in zip(ids, values) if value}

    async def get(self, app_id: str, open_id: str) -> bytes | None:
        key = app_id, open_id
        async with self.limit:
            cached = self.cache.get(key)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(key)
                return cached[1]
            if self.session is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
            url = f"https://q.qlogo.cn/qqapp/{quote(app_id, safe='')}/{quote(open_id, safe='')}/100"
            data = None
            try:
                async with self.session.get(url, allow_redirects=False) as response:
                    if response.status == 200:
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > 1024 * 1024:
                                raise ValueError("Avatar too large")
                        data = await asyncio.to_thread(self.normalize, bytes(body))
            except (
                aiohttp.ClientError,
                TimeoutError,
                ValueError,
                OSError,
                Image.DecompressionBombError,
            ):
                pass
            self.cache[key] = time.monotonic() + (21600 if data else 600), data
            self.cache.move_to_end(key)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return data

    @staticmethod
    def normalize(data: bytes) -> bytes:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 4_000_000:
                raise ValueError("Avatar dimensions too large")
            with ImageOps.fit(source.convert("RGB"), (96, 96)) as image, io.BytesIO() as output:
                image.save(output, "PNG")
                return output.getvalue()

    async def close(self):
        if self.session is not None:
            await self.session.close()
        self.cache.clear()
