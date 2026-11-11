"""Grok Bot Twin: keep a Grok bot and its Hermes twin in step (built on the botport CLI).

Modules are pure at import time: nothing here touches the disk, starts a thread or spawns a process.
"""
__all__ = ["awareness", "cli", "config", "demo", "engine", "importer", "model", "safe", "secrets", "service",
           "slash", "store", "timer", "tools"]
