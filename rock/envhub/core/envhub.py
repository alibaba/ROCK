"""EnvHub core implementation"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from rock import env_vars
from rock.deployments.sandbox_validator import DockerSandboxValidator
from rock.envhub.api.schemas import DeleteEnvRequest, EnvInfo, GetEnvRequest, ListEnvsRequest, RegisterRequest
from rock.envhub.database.base import Base
from rock.envhub.database.docker_env import RockDockerEnv
from rock.logger import init_logger

# Configure logging
logger = init_logger(__name__)


class EnvHub(ABC):
    """EnvHub abstract base class"""

    @abstractmethod
    def register(self, request: RegisterRequest) -> EnvInfo:
        """
        Register or update environment

        Args:
            request: Registration request

        Returns:
            Environment information
        """
        pass

    @abstractmethod
    def get_env(self, request: GetEnvRequest) -> EnvInfo:
        """
        Get environment

        Args:
            request: Get environment request

        Returns:
            Environment information
        """
        pass

    @abstractmethod
    def list_envs(self, request: ListEnvsRequest) -> list[EnvInfo]:
        """
        List environments

        Args:
            request: List environments request

        Returns:
            List of environment information
        """
        pass

    @abstractmethod
    def delete_env(self, request: DeleteEnvRequest) -> bool:
        """
        Delete environment

        Args:
            request: Delete environment request

        Returns:
            Returns True if deletion is successful, otherwise returns False
        """
        pass

    @abstractmethod
    def check_envs_available(self) -> bool:
        """
        Check if all existing environments have their docker images available locally.
        This checks if the docker image for each environment exists locally.

        Returns:
            True if all environment docker images are available, False if any is not available
        """
        pass


class DockerEnvHub(EnvHub):
    """Docker environment Hub class, inherited from EnvHub"""

    DEFAULT_ENV_NAME = "EnvhubDefaultDockerImage"

    def __init__(self, db_url: str | None = None, validator: DockerSandboxValidator | None = None):
        """
        Initialize DockerEnvHub

        Args:
            db_url: Database URL
        """
        if not db_url:
            db_url = env_vars.ROCK_ENVHUB_DB_URL
        if not validator:
            validator = DockerSandboxValidator()

        self.db_url = db_url
        self.validator = validator

        self.engine = create_engine(db_url, echo=False)
        Base.metadata.create_all(self.engine)

        # pre-check env
        self.init_default_env()

    def init_default_env(self):
        """
        Initialize default environment
        Checks if an environment with env_name DEFAULT_ENV_NAME exists in db,
        creates it if it doesn't exist using the register method
        """
        default_docker_image = env_vars.ROCK_ENVHUB_DEFAULT_DOCKER_IMAGE

        if not default_docker_image:
            logger.warning("No default docker image specified, skipping initialization")
            return

        register_request = RegisterRequest(
            env_name=self.DEFAULT_ENV_NAME,
            image=default_docker_image,
            owner="ENVHUB",
            description="Default docker environment provided by EnvHub",
            tags=["default", "system", "envhub"],
            extra_spec={},
        )
        self.register(register_request)
        logger.info(f"Created default environment: {self.DEFAULT_ENV_NAME} with image {default_docker_image}")

    def check_envs_available(self) -> bool:
        """
        Check if all existing environments have their docker images available locally.
        This checks if the docker image for each environment exists locally using docker CLI.

        Returns:
            True if all environment docker images are available, False if any is not available
        """
        # Check if docker command is available
        if not self.validator.check_availability():
            logger.error("Docker command not found, cannot check environment availability")
            return False

        # Get all environments from database
        with self.get_session() as session:
            all_envs = session.query(RockDockerEnv).all()

            for db_env in all_envs:
                image = db_env.image
                # Use docker inspect to check if image exists
                if not self.validator.check_resource(image):
                    logger.error(f"Docker image {image} not found")
                    return False

        return True

    @contextmanager
    def get_session(self):
        """Context manager for database sessions.
        Provides a SQLAlchemy session that automatically handles commit/rollback
        and ensures proper cleanup of resources.
        Yields:
            A SQLAlchemy session object.
        """
        session = Session(self.engine)
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Session rollback due to error: {e}")
            raise
        finally:
            session.close()

    def register(self, request: RegisterRequest) -> EnvInfo:
        """
        Register or update environment

        Args:
            request: Registration request

        Returns:
            Environment information
        """
        logger.info(f"Registering environment: {request.env_name}")
        with self.get_session() as session:
            try:
                # Check if environment already exists
                db_env = session.query(RockDockerEnv).filter(RockDockerEnv.env_name == request.env_name).first()
                if db_env:
                    # Update existing environment
                    db_env.image = request.image
                    db_env.owner = request.owner
                    db_env.description = request.description
                    db_env.tags = request.tags
                    db_env.extra_spec = request.extra_spec
                    db_env.update_at = datetime.now()
                else:
                    # Create new environment
                    db_env = RockDockerEnv(
                        env_name=request.env_name,
                        image=request.image,
                        owner=request.owner,
                        description=request.description,
                        tags=request.tags,
                        extra_spec=request.extra_spec,
                    )
                    session.add(db_env)

                session.commit()
                session.refresh(db_env)

                return EnvInfo(
                    env_name=db_env.env_name,
                    image=db_env.image,
                    owner=db_env.owner,
                    description=db_env.description,
                    tags=db_env.tags if db_env.tags else [],
                    extra_spec=db_env.extra_spec,
                    create_at=db_env.create_at,
                    update_at=db_env.update_at,
                )
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to register environment {request.env_name}: {e}")
                raise

    def get_env(self, request: GetEnvRequest) -> EnvInfo:
        """
        Get environment

        Args:
            request: Get environment request

        Returns:
            Environment information
        """
        logger.info(f"Getting environment: {request.env_name}")
        with self.get_session() as session:
            db_env = session.query(RockDockerEnv).filter(RockDockerEnv.env_name == request.env_name).first()
            if not db_env:
                raise Exception(f"Environment {request.env_name} not found")

            return EnvInfo(
                env_name=db_env.env_name,
                image=db_env.image,
                owner=db_env.owner,
                description=db_env.description,
                tags=db_env.tags if db_env.tags else [],
                extra_spec=db_env.extra_spec,
                create_at=db_env.create_at,
                update_at=db_env.update_at,
            )

    def list_envs(self, request: ListEnvsRequest) -> list[EnvInfo]:
        """
        List environments

        Args:
            request: List environments request

        Returns:
            List of environment information
        """
        logger.info(f"Listing environments with owner={request.owner}, tags={request.tags}")
        with self.get_session() as session:
            query = session.query(RockDockerEnv)
            if request.owner:
                query = query.filter(RockDockerEnv.owner == request.owner)
            if request.tags:
                # Filter environments that have any of the specified tags
                filtered_envs = []
                all_envs = query.all()
                for env in all_envs:
                    if env.tags and any(tag in env.tags for tag in request.tags):
                        filtered_envs.append(env)
                envs = []
                for db_env in filtered_envs:
                    envs.append(
                        EnvInfo(
                            env_name=db_env.env_name,
                            image=db_env.image,
                            owner=db_env.owner,
                            description=db_env.description,
                            tags=db_env.tags if db_env.tags else [],
                            extra_spec=db_env.extra_spec,
                            create_at=db_env.create_at,
                            update_at=db_env.update_at,
                        )
                    )
                return envs

            db_envs = query.all()
            envs = []
            for db_env in db_envs:
                envs.append(
                    EnvInfo(
                        env_name=db_env.env_name,
                        image=db_env.image,
                        owner=db_env.owner,
                        description=db_env.description,
                        tags=db_env.tags if db_env.tags else [],
                        extra_spec=db_env.extra_spec,
                        create_at=db_env.create_at,
                        update_at=db_env.update_at,
                    )
                )

            return envs

    def delete_env(self, request: DeleteEnvRequest) -> bool:
        """
        Delete environment

        Args:
            request: Delete environment request

        Returns:
            Returns True if deletion is successful, otherwise returns False
        """
        logger.info(f"Deleting environment: {request.env_name}")
        with self.get_session() as session:
            db_env = session.query(RockDockerEnv).filter(RockDockerEnv.env_name == request.env_name).first()
            if not db_env:
                return False

            session.delete(db_env)
            session.commit()
            return True
