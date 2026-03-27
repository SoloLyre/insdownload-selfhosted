from __future__ import annotations

from app.platforms.base import PlatformAdapter
from app.platforms.douyin import DOUYIN_ADAPTER
from app.platforms.instagram import INSTAGRAM_ADAPTER
from app.platforms.xiaohongshu import XIAOHONGSHU_ADAPTER


PLATFORM_REGISTRY: dict[str, PlatformAdapter] = {
    INSTAGRAM_ADAPTER.platform_id: INSTAGRAM_ADAPTER,
    DOUYIN_ADAPTER.platform_id: DOUYIN_ADAPTER,
    XIAOHONGSHU_ADAPTER.platform_id: XIAOHONGSHU_ADAPTER,
}


def platform_choices() -> list[PlatformAdapter]:
    return list(PLATFORM_REGISTRY.values())


def get_adapter(platform_id: str) -> PlatformAdapter:
    try:
        return PLATFORM_REGISTRY[platform_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {platform_id}") from exc
