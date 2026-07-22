"""Base transformer — applied during transfer for masking / redaction /
format conversion / etc.

A transformer is invoked on the byte stream between source `get()` and
destination `put()`. Multiple transformers can be chained; each is a
plain function: (bytes, params) -> bytes.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, Tuple


class TransformerError(Exception):
    pass


class BaseTransformer(abc.ABC):
    kind: str = ""

    @abc.abstractmethod
    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes: ...

    @classmethod
    @abc.abstractmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "BaseTransformer": ...