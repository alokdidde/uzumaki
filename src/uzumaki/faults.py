"""Failure signals raised by providers, tools and workers into a bounded run.

Messages are fixed codes; they never echo requests, results or configuration.
"""

from __future__ import annotations


class TransientProviderError(Exception):
    """A provider call failed in a way that may succeed on retry (rate limit, disconnect)."""


class ProviderTimeout(TransientProviderError):
    """A provider or tool call did not return before its deadline."""


class RateLimited(TransientProviderError):
    """The provider refused the call for a rate or capacity limit. Retried within budget."""


class ProviderDisconnected(TransientProviderError):
    """The connection or stream closed before a complete reply. Retried within budget."""


class WorkerFailure(Exception):
    """A provider or tool worker crashed or returned unusable output. Never retried."""
