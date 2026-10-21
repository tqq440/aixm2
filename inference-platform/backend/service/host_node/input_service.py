"""瞬态输入材料：校验摘要及真实媒体，不保存聊天正文，不采用上传文件名。"""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib

from backend.common.exceptions.app_error import AppError
from backend.data.files.upload_store import LocalUploadStore
from backend.schema.host_node.upstream_schema import ExecutionManifest
from backend.schema.llm.online_schema import OnlineRequestInput
from backend.utils.audio.audio_utils import decode_audio_file, encode_pcm16_wav
from backend.utils.image.image_utils import decode_image_file
from backend.utils.json.json_utils import canonical_json_sha256


@dataclass
class ExecutionMaterial:
    manifest: ExecutionManifest | None = None
    content: bytes | None = field(default=None, repr=False)
    online: OnlineRequestInput | None = field(default=None, repr=False)
    prepared: bytes | None = field(default=None, repr=False)

    def validate(self, command, config, root):
        try:
            if command.kind == "llm":
                request = OnlineRequestInput.model_validate(self.online.model_dump())
                if (request.online_request_id != command.input_ref or request.model_id != command.model_id
                        or canonical_json_sha256(request.model_dump(mode="json",by_alias=True)) != command.input_sha256):
                    raise ValueError("在线请求不匹配")
                return
            manifest = ExecutionManifest.model_validate(self.manifest.model_dump())
            if (manifest.kind != command.kind or not self.content or len(self.content) != manifest.size_bytes
                    or hashlib.sha256(self.content).hexdigest() != command.input_sha256
                    or canonical_json_sha256(manifest.model_dump(mode="json", by_alias=True)) != command.manifest_hash):
                raise ValueError("输入指纹不匹配")
            LocalUploadStore._reject_links(root)
            root.mkdir(parents=True,exist_ok=True)
            LocalUploadStore._reject_links(root)
            with TemporaryDirectory(prefix="input-",dir=root) as folder:
                path=Path(folder)/"media.upload";path.write_bytes(self.content)
                if command.kind == "asr":
                    if config["protocol"] == "remote-v2" and manifest.language is not None:
                        raise ValueError("Remote-V2 不声明语言控制能力")
                    audio=decode_audio_file(path,max_samples=300*16000)
                    if abs(audio.duration_seconds-manifest.audio_duration_seconds)>1/16000: raise ValueError("音频时长不符")
                    self.prepared=encode_pcm16_wav(audio)
                else:
                    image=decode_image_file(path,max_file_bytes=10*1024*1024,max_pixels=20_000_000)
                    if (image.width,image.height)!=(manifest.image_width,manifest.image_height):raise ValueError("图片尺寸不符")
                    self.prepared=self.content
        except Exception:
            raise AppError("NODE_INPUT_INVALID", "输入、摘要或媒体参数不符合要求，未调用业务。",422) from None


class CentralMaterialSource:
    def __init__(self, core): self.core=core

    def load(self, lease):
        from backend.data.invocations.invocation_models import InferenceInvocation
        with self.core._transaction() as session:
            row=session.get(InferenceInvocation,lease.invocation_id)
            if not row or row.task_id is None:raise AppError("NODE_FILE_INPUT_REQUIRED","离线输入不存在。",409)
            owner=self.core._owner(session,row)
            args={"kind":row.kind,"sizeBytes":owner.input_size_bytes,"modelVersion":owner.model_version}
            if row.kind=="asr":args.update(language=owner.parameters.get("language"),audioDurationSeconds=owner.audio_duration_seconds)
            else: args.update(imageWidth=owner.image_width,imageHeight=owner.image_height,
                confidenceThreshold=owner.parameters["confidence_threshold"],iouThreshold=owner.parameters["iou_threshold"],maxDetections=owner.parameters["max_detections"])
            reference,digest,kind=owner.input_ref,owner.input_sha256,row.kind
        store=LocalUploadStore(self.core.settings.upload_dir,media_type="audio" if kind=="asr" else "image")
        try:
            with store.resolve_path(reference).open("rb") as source:content=source.read(args["sizeBytes"]+1)
            if len(content)!=args["sizeBytes"] or hashlib.sha256(content).hexdigest()!=digest:raise ValueError()
            return ExecutionMaterial(ExecutionManifest.model_validate(args),content)
        except Exception:raise AppError("NODE_INPUT_UNAVAILABLE","中心输入文件未通过核验，未派发。",503) from None
