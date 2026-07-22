"""Transformer subpackage — data-shape and policy-aware transformations."""
from .base import BaseTransformer, TransformerError
from . import mask, redaction, codec, compress, encrypt

registry: dict = {}


def register(transformer_cls):
    registry[transformer_cls.kind] = transformer_cls
    return transformer_cls


def get(kind: str) -> "BaseTransformer":
    """Look up a transformer class by `kind`. Returns the *class* (call
    `.from_config(cfg)` on the result to construct an instance)."""
    if kind not in registry:
        raise TransformerError(f"no transformer for kind='{kind}'")
    return registry[kind]


def instance(kind: str, cfg: dict) -> "BaseTransformer":
    """Convenience: look up + construct in one call."""
    return get(kind).from_config(cfg)


# Manual registration (avoids the chicken-and-egg between decorator and
# the registry dict — see cdeh/adapters/__init__.py for the same pattern).
register(mask.MaskTransformer)
register(redaction.RedactionTransformer)
register(codec.CodecTransformer)
register(compress.CompressTransformer)
register(encrypt.EncryptTransformer)

__all__ = [
    "BaseTransformer", "TransformerError",
    "MaskTransformer", "RedactionTransformer", "CodecTransformer",
    "CompressTransformer", "EncryptTransformer",
    "registry", "register", "get", "instance",
]