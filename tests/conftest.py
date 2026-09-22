from pathlib import Path

import pytest

from hydradex import HydraDex


@pytest.fixture
def project(tmp_path):
    def write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    return tmp_path, write


@pytest.fixture(scope="module")
def python_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("ty")
    package = root / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .models import Model as Exported\n")
    (package / "models.py").write_text(
        '''from dataclasses import dataclass
from typing import overload

from pathlib import Path
Path(__file__).with_suffix(".executed").touch()

class Base:
    def __init__(self, size: int, /, width: int = 4, *, bias: bool = True):
        """Build a model."""
        pass

class Model(Base):
    """A useful model."""

    @classmethod
    def create(cls, width: int, *, device: str = "cpu") -> "Model":
        return cls(1, width)

@dataclass
class Data:
    name: str
    count: int = 3

def factory(name: str, *, enabled: bool = True, **kwargs: object) -> Model:
    return Model(1)

def positional(size: int, /, *args: object, flag: bool = False, **kwargs: object):
    pass

def positional_only(size: int, /):
    pass

def variadic_only(*args: object, **kwargs: object):
    pass

@overload
def overloaded(value: int, *, count: int = 1) -> int: ...
@overload
def overloaded(value: str, *, suffix: str = "") -> str: ...
def overloaded(value, **kwargs):
    return value
''',
        encoding="utf-8",
    )
    with HydraDex([root]) as engine:
        yield root, engine
