from urllib.parse import urlsplit

from langchain_openai import ChatOpenAI

from app.config import get_settings


def create_llm(*, verifier: bool = False, writer: bool = False) -> ChatOpenAI:
    if verifier and writer:
        raise ValueError("Select only one model stage.")
    settings = get_settings()
    model_name = ((settings.verifier_model_name if verifier else None) or settings.model_name or "").strip()
    if not model_name or model_name.startswith("replace-with-"):
        raise ValueError("Set MODEL_NAME to a model ID supported by your provider before creating the model.")

    # Select credentials and endpoint together, including when legacy values
    # remain in .env, so keys cannot be sent to the wrong provider.
    api_key = (settings.model_api_key or "").strip()
    base_url = (settings.model_base_url or "").strip()
    pair_names = "MODEL_API_KEY and MODEL_BASE_URL"
    if verifier and ((settings.verifier_model_api_key or "").strip() or (settings.verifier_model_base_url or "").strip()):
        api_key = (settings.verifier_model_api_key or "").strip()
        base_url = (settings.verifier_model_base_url or "").strip()
        pair_names = "VERIFIER_MODEL_API_KEY and VERIFIER_MODEL_BASE_URL"
    if api_key or base_url:
        if not api_key or api_key.startswith("replace-with-") or not base_url or base_url.startswith("replace-with-"):
            raise ValueError(f"Set both {pair_names} in the environment or .env.")
    else:
        api_key = (settings.siliconflow_api_key or "").strip()
        base_url = settings.siliconflow_base_url.strip()
        if not api_key or api_key.startswith("replace-with-"):
            raise ValueError("Set MODEL_API_KEY and MODEL_BASE_URL, or SILICONFLOW_API_KEY for the legacy provider.")

    endpoint = urlsplit(base_url)
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("Model base URL must be an HTTP(S) API endpoint without credentials, query or fragment.")

    extra = {}
    if (verifier or writer) and endpoint.hostname.endswith(".aliyuncs.com") and model_name.lower().startswith(("qwen3", "qwen-plus", "qwen-flash")):
        extra["extra_body"] = {"enable_thinking": settings.writer_enable_thinking if writer else settings.verifier_enable_thinking}
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        timeout=settings.model_request_timeout_seconds,
        max_retries=0 if verifier or writer else settings.model_max_retries,
        **extra,
    )


def create_verifier_llm() -> ChatOpenAI:
    return create_llm(verifier=True)


def create_writer_llm() -> ChatOpenAI:
    """Use the research provider/model, with no hidden SDK request retries."""
    return create_llm(writer=True)


def bounded_output_options(model, max_tokens: int) -> dict:
    """Translate output limits at the provider boundary, without mutating a model."""
    host = urlsplit(str(getattr(model, "openai_api_base", "") or "")).hostname
    if host in {"api.siliconflow.cn", "api.siliconflow.com"}:
        return {"extra_body": {**(getattr(model, "extra_body", None) or {}), "max_tokens": max_tokens}}
    return {"max_tokens": max_tokens}
