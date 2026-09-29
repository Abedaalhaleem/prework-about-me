"""Experimental single-person zone estimation (capability C).

DISABLED unless a model has passed the predefined criteria in
``configs/zone_enablement.toml`` on held-out sessions and still matches the
current hardware, room and processing config. See ``docs/ARCHITECTURE.md``.

* :mod:`.criteria`  - strict loader; ``criteria_version`` = SHA-256 of the file
* :mod:`.dataset`   - labelled sessions -> per-window multi-link features
* :mod:`.train`     - session-level splits, train-only fitting, validation-only tuning, one test
* :mod:`.evaluate`  - metrics, criteria checks, the enablement decision
* :mod:`.registry`  - ``<data_dir>/models/<id>.joblib`` + ``.json`` binding
* :mod:`.predictor` - runtime ``DISABLED`` / ``ABSTAIN`` / ``ESTIMATE``
"""

from .criteria import CriteriaError, ZoneCriteria, current_criteria_version, load_criteria
from .dataset import DatasetError, SessionSpec, ZoneDataset, build_dataset, feature_row
from .predictor import ZonePredictor, runtime_inputs
from .registry import ModelBinding, RegistryError, ZoneModelRegistry
from .train import TrainedZoneModel, TrainingRefused, plan_splits, run_training, train_and_evaluate

__all__ = [
    "CriteriaError",
    "ZoneCriteria",
    "current_criteria_version",
    "load_criteria",
    "DatasetError",
    "SessionSpec",
    "ZoneDataset",
    "build_dataset",
    "feature_row",
    "ZonePredictor",
    "runtime_inputs",
    "ModelBinding",
    "RegistryError",
    "ZoneModelRegistry",
    "TrainedZoneModel",
    "TrainingRefused",
    "plan_splits",
    "run_training",
    "train_and_evaluate",
]
