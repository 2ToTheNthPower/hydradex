"""Small real Python targets, requiring no ML framework."""

from dataclasses import dataclass


@dataclass
class Model:
    input_dim: int
    hidden_dim: int = 64
    activation: str = "relu"
    dropout: float = 0.0


@dataclass
class Dataset:
    name: str = "synthetic"
    samples: int = 100
    feature_dim: int = 8
    seed: int = 42


def make_optimizer(lr: float = 0.001, weight_decay: float = 0.0) -> dict[str, float]:
    """A function target illustrating keyword-parameter completion."""
    return {"lr": lr, "weight_decay": weight_decay}
