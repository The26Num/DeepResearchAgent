from langchain_openai import ChatOpenAI

from app.config import get_settings


def create_llm() -> ChatOpenAI:
    settings = get_settings()
    if not settings.siliconflow_api_key or settings.siliconflow_api_key == "replace-with-your-api-key":
        raise ValueError("Set SILICONFLOW_API_KEY in the environment or .env before creating the model.")
    if not settings.model_name or settings.model_name == "replace-with-a-siliconflow-model-id":
        raise ValueError("Set MODEL_NAME to a SiliconFlow model ID before creating the model.")

    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.siliconflow_api_key,
        base_url=settings.siliconflow_base_url,
        temperature=0,
    )
