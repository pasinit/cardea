ARG M1_IMAGE=cardea-m1-acceptance:local
FROM ${M1_IMAGE}
WORKDIR /work
COPY vegeta /usr/local/bin/vegeta
COPY packages/cardea-load packages/cardea-load
RUN uv sync --frozen --project packages/cardea-load
ENTRYPOINT ["/work/packages/cardea-load/.venv/bin/cardea-load"]
