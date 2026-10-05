"""AgentEnvForge: reproducible, isolated software-engineering environments for AI agents.

The package is organised in layers (see ``docs/architecture/components.md``):

* ``domain``      - pure data models, the run state machine and failure taxonomy.
* ``tasks``       - the versioned task specification and its loader/validator.
* ``agents``, ``grading``, ``sandbox`` - public extension points and built-in implementations.
* ``execution``   - the application service that drives one run end to end.
* ``persistence``, ``artifacts``, ``events``, ``telemetry`` - recording what happened.
* ``cli``         - the ``agentenv`` command-line interface.
"""

from agentenvforge._version import __version__

__all__ = ["__version__"]
