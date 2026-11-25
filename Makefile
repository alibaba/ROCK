.PHONY: help init install-hooks check-setup preflight-check test-unit test-integration

# Default target
.DEFAULT_GOAL := help

# Project configuration
PROJECT_NAME := rock
PYTHON_VERSION := 3.11
VENV_PATH := .venv

# Help information
help: ## Show help information
	@echo "$(PROJECT_NAME) - Development Environment Initialization"
	@echo ""
	@echo "Requirements:"
	@echo "  - Docker: Required for containerization"
	@echo "  - uv: Required for dependency management"
	@echo ""
	@echo "Available commands:"
	@echo "  make init           Initialize development environment"
	@echo "  make install-hooks  Install pre-commit hooks"
	@echo "  make check-setup    Check environment setup"
	@echo "  make preflight-check   Check prerequisites for running ROCK admin server"
	@echo ""
	@echo "Note: For distributed environments, ensure consistent Python configurations across all machines."

# Environment initialization
init: ## Initialize development environment
	@echo "Initializing development environment..."
	@if [ ! -d "$(VENV_PATH)" ]; then \
		echo "Creating virtual environment with managed Python..."; \
		uv venv --python $(PYTHON_VERSION) --python-preference only-managed $(VENV_PATH); \
	else \
		echo "Virtual environment already exists, skipping creation"; \
	fi
	@uv sync --all-extras --all-groups
	@$(MAKE) install-hooks
	@$(MAKE) check-setup
	@$(MAKE) preflight-check
	@echo "Development environment initialization completed"
	@echo ""
	@echo "Next steps:"
	@echo "  uv run pytest             # Run tests"
	@echo ""
	@echo "Note: ROCK relies on Docker and uv for environment management."
	@echo "For distributed environments, ensure consistent Python configurations across all machines."

install-hooks: ## Install pre-commit hooks
	@uv run pre-commit install > /dev/null
	@echo "Pre-commit hooks installed"

check-setup: ## Check development environment setup
	@if [ ! -d "$(VENV_PATH)" ]; then \
		echo "Virtual environment does not exist"; \
		exit 1; \
	fi
	@if [ ! -f ".git/hooks/pre-commit" ]; then \
		echo "Pre-commit hooks not installed"; \
		exit 1; \
	fi
	@echo "Environment check passed"

preflight-check: ## Check prerequisites for running ROCK admin server
	@echo "Checking uv installation..."
	@command -v uv >/dev/null 2>&1 || { echo "❌ ERROR: uv is not installed"; echo "   Please install uv: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	@echo "✅ uv is installed"
	@echo ""
	@echo "Checking Docker installation..."
	@command -v docker >/dev/null 2>&1 || { echo "❌ ERROR: Docker is not installed"; echo "   Please install Docker: https://docs.docker.com/get-docker/"; exit 1; }
	@docker version >/dev/null 2>&1 || { echo "❌ ERROR: Docker daemon is not running"; echo "   Please start Docker service and try again"; exit 1; }
	@echo "✅ Docker is installed and accessible"
	@echo ""
	@echo "Checking python:3.11 image..."
	@if docker images python:3.11 --format "table {{.Repository}}\t{{.Tag}}" | grep -q "python\s*3.11"; then \
		echo "✅ python:3.11 image found"; \
	else \
		echo "⚠️  WARNING: python:3.11 image not found"; \
		echo "   You can pull it with: docker pull python:3.11"; \
		echo "   Or run 'make init' to set up your environment properly"; \
	fi
	@echo ""
	@echo "Preflight check completed - ready to run ROCK admin server"

# Testing targets
test-unit: ## Run all unit tests
	@echo "🧪 Running unit tests..."
	@mkdir -p .tmp/test_data/logs
	@uv run pytest tests/unit --timeout=60 --reruns 1 -v

test-integration: ## Run all integration tests in Docker (includes Ray + Docker + Admin)
	@echo "🧪 Running integration tests in Docker..."
	@./scripts/run-integration-tests.sh tests/integration
