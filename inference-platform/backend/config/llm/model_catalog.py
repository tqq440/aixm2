"""显式配置一个兼容 Chat Completions 的服务；配置不代表健康检查通过。"""

from backend.config.settings.app_settings import AppSettings
from backend.schema.models.model_schema import ModelRegistration


def configured_spec(settings: AppSettings) -> ModelRegistration | None:
    if settings.llm_endpoint is None:
        return None
    return ModelRegistration(
        model_id=settings.llm_model_id, model_version=settings.llm_model_version,
        model_repo=settings.llm_upstream_model, task_type="llm", execution_mode="online",
        adapter_key="openai-chat-completions",
        parameter_schema={"defaultTemperature": 0.7, "defaultMaxTokens": 512, "maxTokens": 2048},
        input_constraints={"maxMessages": 32, "maxInputCharacters": 16000},
        output_capabilities=["text", "stream"],
        resource_requirements={"connectionMode": "static-endpoint", "protocol": "chat-completions-sse"},
    )
