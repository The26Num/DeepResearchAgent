import pytest

import app.llm as llm_module
from app.config import Settings


@pytest.fixture
def make_settings(monkeypatch):
    for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    def configure(**values):
        settings = Settings(_env_file=None, **values)
        monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
        return settings

    return configure


def test_generic_credentials_take_precedence_as_a_pair(make_settings):
    make_settings(model_name="qwen3.7-flash", model_api_key="qwen-test-key",
                  model_base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                  siliconflow_api_key="legacy-test-key", model_request_timeout_seconds=45,
                  model_max_retries=1)
    model = llm_module.create_llm()
    assert model.model_name == "qwen3.7-flash"
    assert model.openai_api_key.get_secret_value() == "qwen-test-key"
    assert str(model.client._client.base_url) == "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
    assert model.request_timeout == 45
    assert model.client._client.max_retries == 1


def test_legacy_siliconflow_configuration_still_works(make_settings):
    make_settings(model_name="Qwen/Qwen2.5-7B-Instruct", siliconflow_api_key="legacy-test-key")
    model = llm_module.create_llm()
    assert model.openai_api_key.get_secret_value() == "legacy-test-key"
    assert str(model.client._client.base_url) == "https://api.siliconflow.cn/v1/"


@pytest.mark.parametrize("values", [
    {"model_api_key": "new-test-key"},
    {"model_base_url": "https://provider.example/v1"},
    {"model_api_key": "replace-with-your-api-key", "model_base_url": "https://provider.example/v1"},
])
def test_partial_generic_configuration_never_uses_legacy_credentials(make_settings, values):
    make_settings(model_name="qwen3.7-flash", siliconflow_api_key="legacy-test-key", **values)
    with pytest.raises(ValueError, match="both MODEL_API_KEY and MODEL_BASE_URL"):
        llm_module.create_llm()


@pytest.mark.parametrize("model_name", [None, "", "replace-with-a-siliconflow-model-id"])
def test_missing_model_id_is_rejected(make_settings, model_name):
    make_settings(model_name=model_name, model_api_key="test-key", model_base_url="https://provider.example/v1")
    with pytest.raises(ValueError, match="MODEL_NAME"):
        llm_module.create_llm()


@pytest.mark.parametrize("base_url", ["provider.example/v1", "ftp://provider.example/v1", "https://user:password@provider.example/v1"])
def test_invalid_endpoint_is_rejected_without_echoing_credentials(make_settings, base_url):
    make_settings(model_name="qwen3.7-flash", model_api_key="private-test-key", model_base_url=base_url)
    with pytest.raises(ValueError, match="HTTP\\(S\\)") as error:
        llm_module.create_llm()
    assert "private-test-key" not in str(error.value)
    assert "password" not in str(error.value)


def test_new_credentials_are_loaded_from_dotenv(make_settings, tmp_path):
    make_settings()
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_NAME=qwen3.7-flash\nMODEL_API_KEY=file-test-key\n"
                        "MODEL_BASE_URL=https://provider.example/v1\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.model_api_key == "file-test-key"
    assert settings.model_base_url == "https://provider.example/v1"


def test_no_credentials_reports_supported_configuration(make_settings):
    make_settings(model_name="qwen3.7-flash")
    with pytest.raises(ValueError, match="MODEL_API_KEY and MODEL_BASE_URL"):
        llm_module.create_llm()
