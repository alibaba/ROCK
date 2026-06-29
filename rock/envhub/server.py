"""EnvHub server implementation"""

import argparse
import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI, HTTPException

from rock.envhub.api.schemas import DeleteEnvRequest, GetEnvRequest, ListEnvsRequest, RegisterRequest
from rock.envhub.core.envhub import DockerEnvHub

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Create FastAPI application instance
app = FastAPI(title="EnvHub API", description="Environment management service for ROCK", version="1.0.0")

# Initialize EnvHub instance
env_hub = None


def initialize_env_hub(db_url):
    """Initialize EnvHub instance, only supports database URL configuration"""
    global env_hub
    env_hub = DockerEnvHub(db_url=db_url)
    logger.info(f"EnvHub initialized with db_url: {db_url}")


@app.post("/env/register")
async def register_env(request: RegisterRequest):
    """Register or update environment"""
    try:
        logger.info(f"Registering environment: {request.env_name}")
        env = env_hub.register(request)
        return env
    except Exception as e:
        logger.error(f"Error registering environment {request.env_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/env/get")
async def get_env(request: GetEnvRequest):
    """Get environment by name"""
    try:
        logger.info(f"Getting environment: {request.env_name}")
        env = env_hub.get_env(request)
        return env
    except Exception as e:
        logger.error(f"Error getting environment {request.env_name}: {e}")
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/env/list")
async def list_envs(request: ListEnvsRequest):
    """List environments, support filtering by owner and tags"""
    try:
        logger.info(f"Listing environments with owner={request.owner}, tags={request.tags}")
        envs = env_hub.list_envs(request)
        return {"envs": envs}
    except Exception as e:
        logger.error(f"Error listing environments: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/env/delete")
async def delete_env(request: DeleteEnvRequest):
    """Delete environment"""
    try:
        logger.info(f"Deleting environment: {request.env_name}")
        result = env_hub.delete_env(request)
        if not result:
            raise HTTPException(status_code=404, detail=f"Environment {request.env_name} not found or already deleted")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting environment {request.env_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


def main():
    """Main function, supports command line arguments"""
    # Set uvloop as global event loop policy
    if sys.platform != "win32":
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    parser = argparse.ArgumentParser(description="EnvHub Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind to (default: 8081)")
    parser.add_argument(
        "--db-url", default="sqlite:///rock_envs.db", help="Database URL (default: sqlite:///rock_envs.db)"
    )

    args = parser.parse_args()

    # Initialize EnvHub
    initialize_env_hub(db_url=args.db_url)

    # Start server
    uvicorn.run(app, host=args.host, port=args.port, reload=False, log_level="info", loop="uvloop", http="httptools")


if __name__ == "__main__":
    main()
