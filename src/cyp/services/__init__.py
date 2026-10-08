"""Service layer: the operations the CLI and the HTTP API expose, on top of the pure models.

Each function takes an :class:`~cyp.services.context.AppContext`, returns pydantic models from
:mod:`cyp.schemas` (the frontend contract) and never prints. Recomputes read the cached
:class:`~cyp.dataset.Dataset`, so repeated calls cost milliseconds.
"""
