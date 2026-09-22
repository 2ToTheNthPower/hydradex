"""Compose the configs and instantiate their Python targets with real Hydra."""

import json
from dataclasses import asdict

import hydra
from hydra.utils import instantiate
from omegaconf import DictConfig


@hydra.main(version_base=None, config_path="conf", config_name="config")
def main(config: DictConfig) -> None:
    model = instantiate(config.model)
    dataset = instantiate(config.dataset)
    optimizer = instantiate(config.optimizer)
    print(
        json.dumps(
            {
                "model": asdict(model),
                "dataset": asdict(dataset),
                "optimizer": optimizer,
                "training": {
                    "epochs": config.training.epochs,
                    "batch_size": config.training.batch_size,
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
