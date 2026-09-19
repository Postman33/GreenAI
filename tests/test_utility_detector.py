from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ezdxf
import numpy as np
from shapely.geometry import LineString
from sklearn.ensemble import RandomForestClassifier

from tests import ROOT  # noqa: F401 - initializes script-module import paths
from utility_detector import detector, onnx_model


class UtilityDetectorTests(unittest.TestCase):
    def test_labeled_dxf_reads_explicit_green_as_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labeled.dxf"
            document = ezdxf.new("R2018")
            document.layers.add("Сущ_сети_Газопровод")
            modelspace = document.modelspace()
            modelspace.add_line(
                (0, 0),
                (10, 0),
                dxfattribs={"layer": "Сущ_сети_Газопровод", "color": 3},
            )
            modelspace.add_line(
                (2, -1),
                (2, 1),
                dxfattribs={"layer": "Сущ_сети_Газопровод", "color": 256},
            )
            document.saveas(path)

            grouped = detector.load_labeled_dxf(path, {"gas_pipe"}, 3, 0.1)

            self.assertEqual(len(grouped["gas_pipe"]), 2)
            self.assertEqual([item.label for item in grouped["gas_pipe"]], [1, 0])

    def test_feature_matrix_contains_graph_context(self) -> None:
        primitives = [
            detector.Primitive(LineString([(0, 0), (10, 0)]), "gas_pipe", "LINE", "gas"),
            detector.Primitive(LineString([(10, 0), (20, 0)]), "gas_pipe", "LINE", "gas"),
            detector.Primitive(LineString([(5, -0.5), (5, 0.5)]), "gas_pipe", "LINE", "gas"),
        ]

        features = detector.build_feature_matrix(primitives, connect_tolerance=0.1, marker_max_length=2.0)

        self.assertEqual(features.shape, (3, len(detector.FEATURE_NAMES)))
        self.assertTrue(np.isfinite(features).all())
        perpendicular_index = detector.FEATURE_NAMES.index("short_perpendicular_count")
        self.assertGreaterEqual(features[0, perpendicular_index], 1)
        continuation_index = detector.FEATURE_NAMES.index("best_continuation")
        self.assertGreater(features[0, continuation_index], 0.9)

    def test_feature_matrix_does_not_connect_different_drawings(self) -> None:
        primitives = [
            detector.Primitive(
                LineString([(0, 0), (10, 0)]),
                "water_pipe",
                "LINE",
                "water",
                dataset_id="drawing-a",
            ),
            detector.Primitive(
                LineString([(10, 0), (20, 0)]),
                "water_pipe",
                "LINE",
                "water",
                dataset_id="drawing-b",
            ),
        ]

        features = detector.build_feature_matrix(primitives, connect_tolerance=0.1)

        start_degree = detector.FEATURE_NAMES.index("start_degree")
        end_degree = detector.FEATURE_NAMES.index("end_degree")
        self.assertEqual(features[0, start_degree], 0)
        self.assertEqual(features[0, end_degree], 0)
        self.assertEqual(features[1, start_degree], 0)
        self.assertEqual(features[1, end_degree], 0)

    def test_threshold_prefers_recall_target(self) -> None:
        labels = np.asarray([1, 1, 1, 0, 0])
        probabilities = np.asarray([0.95, 0.80, 0.40, 0.35, 0.10])
        threshold = detector.choose_threshold(labels, probabilities, 1.0)
        result = detector.metrics(labels, probabilities, threshold)
        self.assertEqual(result["recall"], 1.0)
        self.assertGreaterEqual(result["precision"], 0.75)

    def test_threshold_can_enforce_precision_target(self) -> None:
        labels = np.asarray([1, 1, 1, 0, 0])
        probabilities = np.asarray([0.95, 0.80, 0.40, 0.70, 0.10])
        threshold = detector.choose_threshold_for_precision(
            labels, probabilities, 0.75
        )
        result = detector.metrics(labels, probabilities, threshold)
        self.assertGreaterEqual(result["precision"], 0.75)
        self.assertEqual(result["recall"], 1.0)

    def test_decision_thresholds_can_retain_uncertain_band(self) -> None:
        accepted_threshold = 0.4
        review_threshold = accepted_threshold * 0.7
        probabilities = [0.6, 0.35, 0.1]
        decisions = [
            "accepted" if value >= accepted_threshold else
            "manual_review" if value >= review_threshold else
            "rejected"
            for value in probabilities
        ]
        self.assertEqual(decisions, ["accepted", "manual_review", "rejected"])

    def test_decision_metrics_counts_primitives_and_lengths(self) -> None:
        classified = [
            (
                detector.Primitive(
                    LineString([(0, 0), (10, 0)]), "gas_pipe", "LINE", "gas", label=1
                ),
                0.9,
                "accepted",
            ),
            (
                detector.Primitive(
                    LineString([(0, 1), (5, 1)]), "gas_pipe", "LINE", "gas", label=0
                ),
                0.8,
                "accepted",
            ),
            (
                detector.Primitive(
                    LineString([(0, 2), (2, 2)]), "gas_pipe", "LINE", "gas", label=1
                ),
                0.1,
                "rejected",
            ),
        ]

        result = detector.decision_metrics(classified, {"accepted"})

        self.assertEqual(result["true_positive"], 1)
        self.assertEqual(result["false_positive"], 1)
        self.assertEqual(result["false_negative"], 1)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 0.5)
        self.assertAlmostEqual(result["length_precision"], 10 / 15)
        self.assertAlmostEqual(result["length_recall"], 10 / 12)

    def test_onnx_bundle_matches_in_memory_classifier(self) -> None:
        primitives = [
            detector.Primitive(
                LineString([(index * 10.0, 0), (index * 10.0 + length, 0)]),
                "gas_pipe",
                "LINE",
                "gas",
                dataset_id="test",
            )
            for index, length in enumerate([1.0, 2.0, 5.0, 10.0] * 8)
        ]
        features = detector.build_feature_matrix(primitives, connect_tolerance=0.1)
        labels = np.asarray([0, 0, 1, 1] * 8)
        classifier = RandomForestClassifier(n_estimators=20, random_state=42)
        classifier.fit(features, labels)
        training_bundle = {
            "feature_names": detector.FEATURE_NAMES,
            "connect_tolerance": 0.1,
            "marker_max_length": 2.0,
            "models": {
                "gas_pipe": {
                    "classifier": classifier,
                    "accepted_threshold": 0.6,
                    "review_threshold": 0.3,
                }
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            bundle_path = Path(directory) / "model"
            metadata = onnx_model.export_bundle(
                training_bundle,
                bundle_path,
                verification_features={"gas_pipe": features},
            )
            runtime = onnx_model.load_bundle(bundle_path)
            expected, _ = detector.classify(training_bundle, {"gas_pipe": primitives})
            actual, _ = detector.classify_onnx(runtime, {"gas_pipe": primitives})

        self.assertTrue(
            metadata["networks"]["gas_pipe"]["verification"][
                "accepted_decisions_match"
            ]
        )
        self.assertEqual(
            [decision for _, _, decision in expected["gas_pipe"]],
            [decision for _, _, decision in actual["gas_pipe"]],
        )
        np.testing.assert_allclose(
            [probability for _, probability, _ in expected["gas_pipe"]],
            [probability for _, probability, _ in actual["gas_pipe"]],
            atol=1e-6,
        )


if __name__ == "__main__":
    unittest.main()
