"""Reasoning-engine interface shared by all backends (SRS 5.2 / 8.4).

The contract is identical across backends; only the client and endpoint differ:
    summarize(messages) -> list[Summary]
"""
