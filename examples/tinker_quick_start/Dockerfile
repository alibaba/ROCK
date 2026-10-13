# Public ROCKlet/Harbor controller with a fixed, task-independent agent payload.
FROM python:3.12-slim-bookworm
ENV PIP_INDEX_URL=https://pypi.org/simple PIP_EXTRA_INDEX_URL="" PIP_CONFIG_FILE=/dev/null \
    PIP_NO_CACHE_DIR=1 PYTHON_DOTENV_DISABLED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True LITELLM_TELEMETRY=False
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates libseccomp2 iptables iproute2 procps \
    && rm -rf /var/lib/apt/lists/*
COPY docker/ /usr/local/bin/
COPY docker-compose-linux-x86_64 /usr/local/lib/docker/cli-plugins/docker-compose
COPY requirements-rocklet.txt /opt/requirements-rocklet.txt
COPY wheels/ /opt/wheels/
ARG INSTALL_OFFLINE=0
RUN python -m venv /opt/rocklet \
    && if [ "$INSTALL_OFFLINE" = 1 ]; then export PIP_NO_INDEX=1 PIP_FIND_LINKS=/opt/wheels; fi \
    && /opt/rocklet/bin/python -m pip install /opt/wheels/rl_rock-*.whl -r /opt/requirements-rocklet.txt
ENV PATH=/opt/rocklet/bin:/usr/local/bin:$PATH DOCKER_HOST=unix:///var/run/docker.sock
COPY public_swe_agent.py /opt/rocklet/lib/python3.12/site-packages/public_swe_agent.py
RUN chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose \
    && /opt/rocklet/bin/python -c 'import rock.rocklet.server, rock.sdk.model.server.main, harbor, openai; from public_swe_agent import PreinstalledSweAgent' \
    && docker --version && docker compose version
COPY sweagent-runtime.tar.gz /opt/public/sweagent-runtime.tar.gz
WORKDIR /workspace
EXPOSE 22555
ENTRYPOINT ["/opt/rocklet/bin/rocklet"]
CMD ["--port", "22555"]
