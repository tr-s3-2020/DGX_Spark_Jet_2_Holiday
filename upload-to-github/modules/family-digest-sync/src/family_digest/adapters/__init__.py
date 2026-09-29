"""渠道与模型润色的可替换适配器。"""

from .channel import Channel, ConsoleChannel, FlakyChannel, MockChannel
from .llm import MockSummarizer, NullSummarizer

__all__ = ["Channel", "ConsoleChannel", "FlakyChannel", "MockChannel",
           "MockSummarizer", "NullSummarizer"]
