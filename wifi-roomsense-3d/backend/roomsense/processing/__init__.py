"""RoomSense signal processing (capability B: motion detection from measured CSI).

Pipeline: CSI frame -> amplitude sample -> gap-aware window -> quality report
-> features -> comparison with a user-recorded quiet baseline -> hysteresis
state. The activity score is a unitless heuristic, not a probability. Low
quality or stale data gives ``UNKNOWN`` / ``SENSOR_OFFLINE``, never "no motion".
"""

from .amplitude import AmplitudeSample, FrameRejection, convert_frame, frame_to_amplitude
from .baseline import Baseline, BaselineRecorder
from .detector import MotionDetector
from .drift import DriftMonitor, profile_correlation
from .features import FEATURE_NAMES, FeatureVector, extract_features
from .pipeline import ProcessingEngine
from .quality import assess_quality, quality_at_least
from .windows import Window, WindowRejection, build_window

__all__ = [
    "AmplitudeSample",
    "FrameRejection",
    "convert_frame",
    "frame_to_amplitude",
    "Window",
    "WindowRejection",
    "build_window",
    "assess_quality",
    "quality_at_least",
    "FEATURE_NAMES",
    "FeatureVector",
    "extract_features",
    "Baseline",
    "BaselineRecorder",
    "MotionDetector",
    "DriftMonitor",
    "profile_correlation",
    "ProcessingEngine",
]
