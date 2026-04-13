import uvicorn

from orchestrator.app import app
from orchestrator.config import settings


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
