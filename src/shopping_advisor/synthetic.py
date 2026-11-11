"""Original fictional laptops only: no real brands, reviews, or market data."""
from __future__ import annotations

from .catalog import validate_product
from .schemas import DomainError


def seed_data(now: int) -> dict:
    """Build a fresh complete catalog captured 60 seconds before the given epoch second."""
    if type(now) is not int or now < 60:
        raise DomainError("invalid_now", "合成目录时间必须为大于等于 60 的整数秒")

    common = {
        "category": "laptop",
        "synthetic": True,
        "shipping_cents": 0,
        "stock": 10,
        "ram_gb": 16,
        "storage_gb": 512,
        "weight_g": 1500,
        "battery_minutes": 540,
        "cpu_score": 65,
        "gpu_score": 25,
        "discrete_gpu": False,
        "os": "windows",
        "captured_at": now - 60,
    }
    designs = [
        {
            "id": "demo-morning-14", "name": "合成·晨页 14", "price_cents": 249_000,
            "ram_gb": 8, "storage_gb": 256, "weight_g": 1650, "battery_minutes": 420,
            "cpu_score": 42, "gpu_score": 18,
        },
        {
            "id": "demo-square-15", "name": "合成·方格 15", "price_cents": 329_000,
            "shipping_cents": 1500, "weight_g": 1750, "battery_minutes": 510,
            "cpu_score": 60, "gpu_score": 24,
        },
        {
            "id": "demo-feather-13", "name": "合成·羽舟 13", "price_cents": 439_000,
            "weight_g": 990, "battery_minutes": 660, "cpu_score": 56, "gpu_score": 20,
        },
        {
            "id": "demo-longday-14", "name": "合成·长昼 14", "price_cents": 459_000,
            "shipping_cents": 1000, "weight_g": 1350, "battery_minutes": 1080,
            "cpu_score": 50, "gpu_score": 17,
        },
        {
            "id": "demo-forge-16", "name": "合成·炉心 16", "price_cents": 599_000,
            "shipping_cents": 1000, "ram_gb": 32, "storage_gb": 1024,
            "weight_g": 2400, "battery_minutes": 240, "cpu_score": 96, "gpu_score": 78,
            "discrete_gpu": True,
        },
        {
            "id": "demo-arena-16", "name": "合成·棋境 16", "price_cents": 719_000,
            "shipping_cents": 3000, "ram_gb": 32, "storage_gb": 1024,
            "weight_g": 2650, "battery_minutes": 180, "cpu_score": 92, "gpu_score": 96,
            "discrete_gpu": True,
        },
        {
            "id": "demo-canvas-15", "name": "合成·绘岸 15", "price_cents": 559_000,
            "shipping_cents": 2000, "ram_gb": 32, "storage_gb": 1024,
            "weight_g": 2050, "battery_minutes": 390, "cpu_score": 84, "gpu_score": 82,
            "discrete_gpu": True,
        },
        {
            "id": "demo-workshop-14", "name": "合成·码坊 14", "price_cents": 399_000,
            "shipping_cents": 1500, "ram_gb": 32, "storage_gb": 1024,
            "weight_g": 1550, "battery_minutes": 600, "cpu_score": 79, "gpu_score": 28,
            "os": "linux",
        },
        {
            "id": "demo-tide-14", "name": "合成·潮汐 14", "price_cents": 579_000,
            "shipping_cents": 1500, "weight_g": 1250, "battery_minutes": 900,
            "cpu_score": 78, "gpu_score": 52, "os": "macos",
        },
        {
            "id": "demo-freight-edge", "name": "合成·邮界 15", "price_cents": 599_000,
            "shipping_cents": 2000, "weight_g": 1700, "battery_minutes": 570,
            "cpu_score": 82, "gpu_score": 44,
        },
        {
            "id": "demo-unknown-memory", "name": "合成·待补内存 14", "price_cents": 289_000,
            "ram_gb": None, "weight_g": 1400, "battery_minutes": 480,
            "cpu_score": 58, "gpu_score": 22,
        },
        {
            "id": "demo-unknown-storage", "name": "合成·待补存储 14", "price_cents": 299_000,
            "storage_gb": None, "weight_g": 1450, "battery_minutes": 500,
            "cpu_score": 61, "gpu_score": 23,
        },
        {
            "id": "demo-unknown-extras", "name": "合成·待补选配 15", "price_cents": 319_000,
            "weight_g": None, "battery_minutes": None, "cpu_score": None, "gpu_score": None,
            "discrete_gpu": None, "os": None,
        },
        {
            "id": "demo-sold-out", "name": "合成·空架 14", "price_cents": 269_000,
            "stock": 0, "weight_g": 1100, "battery_minutes": 780,
            "cpu_score": 80, "gpu_score": 40,
        },
        {
            "id": "demo-tower-17", "name": "合成·高塔 17", "price_cents": 899_000,
            "shipping_cents": 5000, "ram_gb": 64, "storage_gb": 2048,
            "weight_g": 3200, "battery_minutes": 150, "cpu_score": 100, "gpu_score": 100,
            "discrete_gpu": True, "os": "linux",
        },
        {
            "id": "demo-sprout-13", "name": "合成·初芽 13", "price_cents": 189_000,
            "ram_gb": 4, "storage_gb": 128, "weight_g": 1200, "battery_minutes": 360,
            "cpu_score": 30, "gpu_score": 12, "os": "linux",
        },
    ]
    return {"products": [validate_product({**common, **design}) for design in designs]}
