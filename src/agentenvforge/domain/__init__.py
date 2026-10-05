"""Domain layer: the vocabulary of AgentEnvForge.

Modules here are pure: they must not import Docker, SQLite, Typer, Rich or any other
infrastructure. Everything else in the package depends on this layer, never the
reverse (see ADR-006).
"""
