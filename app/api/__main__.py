import uvicorn

from app.config import get_settings


def main():
    settings = get_settings()
    uvicorn.run("app.api.main:app", host=settings.api_host, port=settings.api_port, workers=1)


if __name__ == "__main__":
    main()
