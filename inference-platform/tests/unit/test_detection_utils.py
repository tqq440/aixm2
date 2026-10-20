"""数值契约测试使用人工候选，不能作为真实YOLO检测证据。"""

import numpy as np
import pytest

from backend.utils.image.detection_utils import DetectionOutputError, letterbox_rgb, postprocess_yolov5


def raw_predictions(rows=()):
    prediction = np.zeros((1, 25200, 85), dtype=np.float16)
    for index, (xywh, objectness, class_id, probability) in enumerate(rows):
        prediction[0, index, :4] = xywh
        prediction[0, index, 4] = objectness
        prediction[0, index, class_id + 5] = probability
    return prediction


def postprocess(prediction, transform=None, **overrides):
    transform = transform or letterbox_rgb(np.zeros((640, 640, 3), dtype=np.uint8))
    parameters = dict(confidence_threshold=0.25, iou_threshold=0.45, max_detections=100)
    parameters.update(overrides)
    return postprocess_yolov5(prediction, transform, **parameters)


def test_letterbox_preserves_rgb_and_inverse_geometry():
    rgb = np.zeros((100, 200, 3), dtype=np.uint8)
    rgb[:, :] = [255, 128, 0]
    value = letterbox_rgb(rgb)
    assert value.tensor.shape == (1, 3, 640, 640)
    assert value.tensor.dtype == np.float16 and value.tensor.flags.c_contiguous
    assert (value.scale, value.pad_x, value.pad_y) == (3.2, 0, 160)
    np.testing.assert_allclose(value.tensor[0, :, 200, 100], [1, 128 / 255, 0], atol=0.0005)
    np.testing.assert_allclose(value.tensor[0, :, 0, 0], [114 / 255] * 3, atol=0.0005)
    assert value.original_width == 200 and value.original_height == 100


def test_non_square_original_coordinates_are_restored_and_clipped():
    transform = letterbox_rgb(np.zeros((100, 200, 3), dtype=np.uint8))
    prediction = raw_predictions([([320, 320, 800, 640], 1, 5, 0.9)])
    result = postprocess(prediction, transform)
    assert len(result) == 1
    box = result[0]
    assert (box.x_min, box.y_min, box.x_max, box.y_max) == (0, 0, 200, 100)
    assert box.class_id == 5


def test_odd_padding_uses_actual_integer_offset():
    value = letterbox_rgb(np.zeros((3, 7, 3), dtype=np.uint8))
    assert value.pad_y == (640 - round(3 * 640 / 7)) // 2
    prediction = raw_predictions([([320, 320, 100, 100], 1, 0, 1)])
    box = postprocess(prediction, value)[0]
    assert box.y_min == pytest.approx((270 - value.pad_y) / value.scale)


def test_scores_multiply_objectness_and_class_probability_before_filtering():
    prediction = raw_predictions([([100, 100, 30, 30], 0.4, 0, 0.5), ([300, 300, 30, 30], 0.9, 5, 0.8)])
    result = postprocess(prediction)
    assert len(result) == 1
    assert result[0].class_id == 5 and result[0].confidence == pytest.approx(0.72, abs=0.001)


def test_nms_removes_same_class_duplicate_but_keeps_different_class():
    prediction = raw_predictions([
        ([100, 100, 80, 80], 1, 0, 0.9),
        ([102, 100, 80, 80], 1, 0, 0.8),
        ([100, 100, 80, 80], 1, 5, 0.7),
    ])
    result = postprocess(prediction)
    assert [box.class_id for box in result] == [0, 5]


def test_no_candidates_is_successful_empty_list_and_threshold_one_is_empty():
    assert postprocess(raw_predictions()) == []
    assert postprocess(raw_predictions([([320, 320, 100, 100], 1, 0, 1)]), confidence_threshold=1) == []


def test_max_detections_applied_globally_in_score_order():
    prediction = raw_predictions([([100, 100, 20, 20], 1, 0, 0.6), ([300, 300, 20, 20], 1, 5, 0.9)])
    result = postprocess(prediction, max_detections=1)
    assert len(result) == 1 and result[0].class_id == 5


def test_boxes_completely_inside_letterbox_padding_not_returned():
    transform = letterbox_rgb(np.zeros((10, 200, 3), dtype=np.uint8))
    assert postprocess(raw_predictions([([20, 20, 10, 10], 1, 0, 1)]), transform) == []


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -np.inf])
def test_nonfinite_rejected_even_when_below_confidence(bad_value):
    raw = raw_predictions()
    raw[0, 20000, 10] = bad_value
    with pytest.raises(DetectionOutputError, match="非有限"):
        postprocess(raw)


@pytest.mark.parametrize("shape", [(25200, 85), (1, 8400, 84), (2, 25200, 85), (1, 25201, 85)])
def test_wrong_shape_rejected(shape):
    with pytest.raises(DetectionOutputError, match="形状"):
        postprocess(np.zeros(shape, dtype=np.float16))


@pytest.mark.parametrize("column,value", [(2, -1), (4, 1.1), (84, -0.1)])
def test_invalid_width_or_probability_rejected(column, value):
    raw = raw_predictions()
    raw[0, 0, column] = value
    with pytest.raises(DetectionOutputError):
        postprocess(raw)


def test_accepted_zero_size_box_is_invalid_not_empty_success():
    with pytest.raises(DetectionOutputError, match="宽高"):
        postprocess(raw_predictions([([100, 100, 0, 20], 1, 0, 1)]))


def test_candidate_budget_rejects_oversized_raw_input_before_nms():
    with pytest.raises(DetectionOutputError, match="上限"):
        postprocess(raw_predictions(), max_candidates=100)


@pytest.mark.parametrize("overrides", [dict(confidence_threshold=float("nan")), dict(iou_threshold=1.1), dict(max_detections=0), dict(max_detections=True), dict(class_count=81)])
def test_invalid_parameters_rejected(overrides):
    with pytest.raises(ValueError):
        postprocess(raw_predictions(), **overrides)


@pytest.mark.parametrize("rgb", [np.zeros((10, 10)), np.zeros((10, 10, 4), dtype=np.uint8), np.zeros((0, 10, 3), dtype=np.uint8)])
def test_bad_rgb_shape_rejected(rgb):
    with pytest.raises(ValueError):
        letterbox_rgb(rgb)


def test_nms_deterministic_equal_scores_keep_first_candidate():
    prediction = raw_predictions([([100, 100, 80, 80], 1, 0, 0.9), ([102, 100, 80, 80], 1, 0, 0.9)])
    assert postprocess(prediction)[0].x_min == 60
