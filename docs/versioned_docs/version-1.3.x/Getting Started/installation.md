---
sidebar_position: 3
---

# Installation

This document explains how to install and set up the ROCK development environment using both `uv` and `pip`. The project is a Reinforcement Open Construction Kit that supports various components.

## Using uv (Recommended)

### Quick Install All Dependencies

```bash
# Install all dependencies including optional ones
uv sync --all-extras

# Install development/testing dependencies
uv sync --all-extras --all-groups
```

### Install Different Dependency Groups

#### Core Dependencies Only
```bash
uv sync
```

#### Admin Component Dependencies
```bash
uv sync --extra admin
```

#### Rocklet Execution Environment Dependencies
```bash
uv sync --extra rocklet
```


#### All Dependencies at Once
```bash
uv sync --all-extras
```

#### Development/Testing Dependencies
```bash
uv sync --all-extras --group test
```

## Using pip

### Install from pip source

#### Core Dependencies Only
```bash
pip install rl-rock
```

#### Admin Component Dependencies
```bash
pip install "rl-rock[admin]"
```

#### Rocklet Execution Environment Dependencies
```bash
pip install "rl-rock[rocklet]"
```

#### Builder Dependencies
```bash
pip install "rl-rock[builder]"
```

#### Install All Optional Dependencies
```bash
pip install "rl-rock[all]"
```

### Install with pip from source code

#### Core Dependencies Only
```bash
pip install .
```

#### Admin Component Dependencies
```bash
pip install ".[admin]"
```

#### Rocklet Execution Environment Dependencies
```bash
pip install ".[rocklet]"
```

#### Builder Dependencies
```bash
pip install ".[builder]"
```

#### Install All Optional Dependencies
```bash
pip install ".[all]"
```

## Available Entry Points

The package provides the following command line scripts:

- `rocklet`: ROCK execution environment server (rock.rocklet.server:main)
- `admin`: Admin management server (rock.admin.main:main)
- `envhub`: Environment hub server (rock.envhub.server:main)
- `rock`: Main ROCK command line interface (rock.cli.main:main)

## Development Setup

### Using uv (Recommended)

```bash
# Clone and set up development environment
git clone <repository>
cd ROCK
uv sync --all-extras --group test

# Run tests
uv run pytest

```

### Using pip

```bash
# For development, install in editable mode with all extras
pip install -e ".[all]"

# Or separately
pip install -e .
pip install ".[admin]" ".[rocklet]" ".[builder]"  # Optional extras
```

## Additional Notes

- The project is configured to use the Alibaba cloud PyPI mirror by default: `https://mirrors.aliyun.com/pypi/simple/`
- For local development, running tests requires the `test` dependency group
