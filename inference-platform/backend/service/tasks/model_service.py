"""按 ASR/Vision 能力返回已启用的固定规格，并应用部署上限。"""

from backend.common.exceptions.app_error import AppError
from backend.config.settings.app_settings import AppSettings
from backend.data.database.database import Database
from backend.schema.tasks.task_schema import (InputConstraints, ModelList, ModelListQuery, ModelParameters, ModelSpec,
                                            VisionInputConstraints, VisionModelParameters)
from backend.service.models.model_registry_service import ModelRegistryService
from backend.service.asr.adapter_catalog import configured_asr_plugin
from backend.schema.llm.online_schema import LlmInputConstraints, LlmModelParameters


class ModelService:
    def __init__(self, settings: AppSettings, database: Database):
        self.settings = settings
        self.registry = ModelRegistryService(database, settings)

    def list_models(self, task_type: str = "asr") -> ModelList:
        query = ModelListQuery(task_type=task_type)
        return ModelList(items=[self._view(spec) for spec in self.registry.list_available()
                                if query.task_type == "all" or spec.task_type == query.task_type])

    def resolve(self, model_id: str, model_version: str | None = None, *, task_type: str = "asr") -> ModelSpec:
        try:
            spec = self.registry.resolve(model_id, model_version)
            if spec.task_type != task_type:
                raise AppError("MODEL_TASK_TYPE_MISMATCH", "该模型不支持此接口的任务类型。", 422)
            return self._view(spec)
        except AppError as exc:
            if exc.status_code == 404:
                raise AppError(exc.code, exc.message, 422) from exc
            raise

    def validate_admission(self, session, model_id: str, model_version: str, *, task_type: str = "asr") -> None:
        # 与任务三表提交共享 SQLite 写事务，禁用和新受理具有确定顺序。
        spec = self.registry.validate_admission(session, model_id, model_version)
        if spec.task_type != task_type:
            raise AppError("MODEL_TASK_TYPE_MISMATCH", "该模型不支持此接口的任务类型。", 422)

    def _view(self, spec) -> ModelSpec:
        if spec.task_type == "llm":
            return ModelSpec(
                model_id=spec.model_id, model_version=spec.model_version, model_repo=spec.model_repo,
                task_type="llm", execution_mode="online", adapter_key=spec.adapter_key,
                parameter_schema=LlmModelParameters.model_validate(spec.parameter_schema),
                input_constraints=LlmInputConstraints.model_validate(spec.input_constraints),
                output_capabilities=spec.output_capabilities, resource_requirements=spec.resource_requirements,
            )
        if spec.task_type == "vision":
            constraints = VisionInputConstraints.model_validate(spec.input_constraints)
            return ModelSpec(
                model_id=spec.model_id, model_version=spec.model_version, model_repo=spec.model_repo,
                task_type=spec.task_type, execution_mode=spec.execution_mode, adapter_key=spec.adapter_key,
                parameter_schema=VisionModelParameters.model_validate(spec.parameter_schema),
                input_constraints=VisionInputConstraints(
                    max_file_bytes=min(constraints.max_file_bytes, self.settings.vision_max_file_bytes),
                    max_pixels=min(constraints.max_pixels, self.settings.vision_max_pixels), formats=constraints.formats,
                ), output_capabilities=spec.output_capabilities, resource_requirements=spec.resource_requirements,
            )
        constraints = InputConstraints.model_validate(spec.input_constraints)
        return ModelSpec(
            model_id=spec.model_id, model_version=spec.model_version,
            version_info=spec.version_info,
            model_repo=spec.model_repo, task_type=spec.task_type,
            execution_mode=spec.execution_mode, adapter_key=spec.adapter_key,
            parameter_schema=ModelParameters.model_validate(spec.parameter_schema),
            input_constraints=InputConstraints(
                max_file_bytes=min(constraints.max_file_bytes, self.settings.asr_max_file_bytes),
                max_audio_seconds=min(constraints.max_audio_seconds, self.settings.asr_max_audio_seconds),
            ),
            output_capabilities=spec.output_capabilities,
            resource_requirements=spec.resource_requirements,
            execution_location="external" if spec.adapter_key == "remote-v2-sensevoice" else "local",
            management_mode="external_call_only" if spec.adapter_key == "remote-v2-sensevoice" else "managed",
        )

    def validate_language(self, language: str | None, model: ModelSpec | None = None) -> None:
        languages = (model.parameter_schema.languages if model is not None
                     else configured_asr_plugin(self.settings).model_spec().parameter_schema["languages"])
        if language is not None and language not in languages:
            raise AppError("LANGUAGE_UNSUPPORTED", "模型不支持此语言；中文使用 zh，自动识别使用 auto。", 422)
