FROM python:3.12-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-21-jre-headless ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 2 https://github.com/spiffe/spire/releases/download/v1.15.3/spire-1.15.3-linux-amd64-musl.tar.gz -o /tmp/spire.tar.gz \
    && echo 'ca1a4d1155317bdd2afc7f36663828a10410c7c840e54725b90b4064b0a301c7  /tmp/spire.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/spire.tar.gz -C /opt && rm /tmp/spire.tar.gz
RUN curl -fsSL --retry 2 https://github.com/keycloak/keycloak/releases/download/26.7.4/keycloak-26.7.4.tar.gz -o /tmp/keycloak.tar.gz \
    && echo '04823c336b797a7e18889a44262a7a64e6bf624cbbcc518c85e2622176ff2eee  /tmp/keycloak.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/keycloak.tar.gz -C /opt && rm /tmp/keycloak.tar.gz
WORKDIR /work
COPY apps/api/pyproject.toml apps/api/uv.lock apps/api/
RUN pip install --no-cache-dir uv==0.11.25 && uv sync --frozen --project apps/api
COPY apps/api/src apps/api/src
COPY apps/api/tests apps/api/tests
COPY experiments/m1 experiments/m1
COPY experiments/spire/binding.py experiments/spire/binding.py
ENV SPIRE_BIN=/opt/spire-1.15.3/bin KEYCLOAK_HOME=/opt/keycloak-26.7.4
CMD ["uv", "run", "--frozen", "--project", "apps/api", "pytest", "-q", "-c", "experiments/m1/pytest.ini", "experiments/m1/tests", "--junitxml=/evidence/debian.xml"]
