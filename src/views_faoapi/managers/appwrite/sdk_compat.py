"""SDK-response normalization helpers — bridge Appwrite SDK <=13 (dicts) and >=14 (Pydantic models).

Extracted from the appwrite god-module (epic #325 S9). No dependency on the SDK client classes,
so it carries no test-mock surface.
"""

from pydantic import BaseModel as _PydanticBaseModel


def _as_dict(obj):
    """Normalize any Appwrite SDK response to a plain dict.

    Works with dicts (SDK <=13), Pydantic models (SDK >=14), and SimpleNamespace.
    For _data-bearing models (Document, Preferences, Row), uses to_dict() and
    flattens the nested 'data' key to match SDK 13's flat shape. Models without
    _data are not flattened, preventing false-positive consumption of a 'data' key.
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, 'to_dict') and callable(obj.to_dict):
        d = obj.to_dict()
        if hasattr(obj, '_data') and isinstance(d.get('data'), dict):
            nested = d.pop('data')
            d.update(nested)
        return d
    if isinstance(obj, _PydanticBaseModel):
        return obj.model_dump(by_alias=True)
    if hasattr(obj, '__dict__'):
        return vars(obj)
    return obj


_SENTINEL = object()


def _get(obj, key):
    """Attribute-or-key access that works with dicts, Pydantic models, and SimpleNamespace.

    For $-prefixed keys (aliases): normalizes via _as_dict() first.
    For regular keys: prefers getattr to preserve rich sub-objects (e.g. Document lists).
    """
    if isinstance(obj, dict):
        return obj.get(key)
    if not key.startswith('$'):
        val = getattr(obj, key, _SENTINEL)
        if val is not _SENTINEL:
            return val
    d = _as_dict(obj)
    if isinstance(d, dict):
        return d.get(key)
    return getattr(obj, key, None)
