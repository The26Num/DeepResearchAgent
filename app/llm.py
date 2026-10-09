from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI

from app.config import get_settings


def create_llm() -> ChatOpenAI:
    settings = get_settings()
    model_name = (settings.model_name or "").strip()
    if not model_name or model_name.startswith("replace-with-"):
        raise ValueError("Set MODEL_NAME to a model ID supported by your provider before creating the model.")

    # Select credentials and endpoint together, including when legacy values
    # remain in .env, so keys cannot be sent to the wrong provider.
    api_key = (settings.model_api_key or "").strip()
    base_url = (settings.model_base_url or "").strip()
    if api_key or base_url:
        if not api_key or api_key.startswith("replace-with-") or not base_url or base_url.startswith("replace-with-"):
            raise ValueError("Set both MODEL_API_KEY and MODEL_BASE_URL in the environment or .env.")
    else:
        api_key = (settings.siliconflow_api_key or "").strip()
        base_url = settings.siliconflow_base_url.strip()
        if not api_key or api_key.startswith("replace-with-"):
            raise ValueError("Set MODEL_API_KEY and MODEL_BASE_URL, or SILICONFLOW_API_KEY for the legacy provider.")

    endpoint = urlsplit(base_url)
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("Model base URL must be an HTTP(S) API endpoint without credentials, query or fragment.")

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        timeout=settings.model_request_timeout_seconds,
        max_retries=settings.model_max_retries,
    )
