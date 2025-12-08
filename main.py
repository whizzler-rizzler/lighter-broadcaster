import uvicorn
from Backend.api import app
from Backend.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "Backend.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info"
    )
