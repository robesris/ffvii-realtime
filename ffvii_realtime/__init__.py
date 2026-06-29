"""ffvii-realtime — remove Tactical Mode slow-motion from Final Fantasy VII
Rebirth combat captures so the whole fight plays at uniform real-time speed."""
from .detect import detect
from .render import render

__version__ = "0.2.4"
__all__ = ["detect", "render", "__version__"]
