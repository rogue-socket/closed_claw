from __future__ import annotations

import json
from dataclasses import MISSING
from typing import Any, Callable


try:
    from pydantic import BaseModel, Field  # type: ignore
except Exception:

    def Field(*, default: Any = MISSING, default_factory: Callable[[], Any] | None = None) -> Any:
        if default_factory is not None:
            return dataclass_field(default_factory=default_factory)
        if default is not MISSING:
            return dataclass_field(default=default)
        return dataclass_field()

    def dataclass_field(*, default: Any = MISSING, default_factory: Callable[[], Any] | None = None):
        from dataclasses import field

        if default_factory is not None:
            return field(default_factory=default_factory)
        if default is not MISSING:
            return field(default=default)
        return field()

    class BaseModel:
        def __init__(self, **kwargs: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for name, annotation in annotations.items():
                if name in kwargs:
                    value = kwargs[name]
                    if (
                        isinstance(value, dict)
                        and isinstance(annotation, type)
                        and issubclass(annotation, BaseModel)
                    ):
                        value = annotation(**value)
                    setattr(self, name, value)
                    continue
                if hasattr(self.__class__, name):
                    setattr(self, name, getattr(self.__class__, name))
                    continue
                raise TypeError(f"Missing required field: {name}")

        @classmethod
        def model_validate_json(cls, value: str):
            data = json.loads(value)
            return cls(**data)

        @classmethod
        def model_validate(cls, value: dict[str, Any]):
            return cls(**value)

        def model_dump(self, **_: Any) -> dict[str, Any]:
            annotations = getattr(self.__class__, "__annotations__", {})
            return {name: _to_plain(getattr(self, name)) for name in annotations}

        def model_dump_json(self, **kwargs: Any) -> str:
            indent = kwargs.get("indent")
            return json.dumps(self.model_dump(), indent=indent)


def _to_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value
