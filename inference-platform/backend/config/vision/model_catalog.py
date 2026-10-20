"""第九部分固定模型；SHA256 来自官方 release 资产的本地实测。"""

MODEL_ID = "yolo-general"
QUEUE_KEY = "vision.yolo-general"
MODEL_VERSION = "yolov5n-v7.0-onnx"
MODEL_FILENAME = "yolov5n.onnx"
MODEL_URL = "https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5n.onnx"
SOURCE_REVISION = "915bbf294bb74c859f0b41f1c23bc395014ea679"
MODEL_SIZE_BYTES = 3_981_910
MODEL_SHA256 = "04f0e55c26f58d17145b36045780fe1250d5bd2187543e11568e5141d05b3262"
INPUT_SIZE = 640
PREPROCESSING_VERSION = "pillow-bilinear-letterbox-v1"
CLASS_NAMES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake",
    "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)

# 核心规范一经登记不可覆盖；部署配置只允许进一步收紧输入上限。
REGISTRY_SPEC = {
    "modelId": MODEL_ID, "modelVersion": MODEL_VERSION,
    "modelRepo": "ultralytics/yolov5", "taskType": "vision", "executionMode": "offline",
    "adapterKey": "yolov5-onnx",
    "parameterSchema": {"defaultConfidenceThreshold": 0.25, "defaultIouThreshold": 0.45,
                        "defaultMaxDetections": 100, "maxDetections": 300},
    "inputConstraints": {"maxFileBytes": 10485760, "maxPixels": 20000000, "formats": ["JPEG", "PNG"]},
    "outputCapabilities": ["detections", "imageDimensions"],
    "resourceRequirements": {"device": "cpu", "inputType": "float16", "maxConcurrency": 1,
                             "modelSha256": MODEL_SHA256},
}
