"""Helper modules injected into the PYTHONPATH of script subprocesses.

The directory containing this package is added to PYTHONPATH by
``tools.execution._build_env`` only when ``READ_ONLY`` is enabled. The
``sitecustomize`` module in this directory is then auto-imported by
Python at interpreter startup and installs guards that refuse mutating
HTTP requests via ``httpx`` (the library used by ``central_helpers``).

This is a defence-in-depth layer on top of the ``BaseHTTPClient`` chokepoint
in :mod:`netops_api_navigator._http_core` — it covers the case where
a script bypasses ``central_helpers`` and instantiates an ``httpx.Client``
directly. It does NOT make script execution a hard security boundary
(scripts still receive OAuth credentials, so a script that uses
``urllib`` / ``requests`` / raw sockets could in principle bypass these
guards). READ_ONLY is primarily an agent behavioural guardrail; do not
rely on it to defend against deliberately malicious scripts.
"""
