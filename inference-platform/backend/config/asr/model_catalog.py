"""首版固定模型能力；模型是否已加载由后续运行状态单独报告。"""

MODEL_ID = "whisper-small"
QUEUE_KEY = "asr.whisper-small"
# small 多语言版本支持 99 种语言；yue 属于较新的模型版本。
SUPPORTED_LANGUAGES = frozenset(
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr "
    "gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv "
    "mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so "
    "sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh".split()
)
