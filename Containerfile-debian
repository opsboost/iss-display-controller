FROM debian:12-slim AS build
LABEL maintainer="Björn Busse <bj.rn@baerlin.eu>"
LABEL org.opencontainers.image.source https://github.com/opsboost/iss-display-controller
LABEL org.label-schema.description="iss display controller"
LABEL org.label-schema.name="iss-display-controller"
LABEL org.label-schema.schema-version="1.0"
LABEL org.label-schema.vcs-url="https://github.com/opsboost/iss-display-controller"

RUN apt-get update && \
    apt-get install --no-install-suggests --no-install-recommends --yes \
    git libxkbcommon-dev pkg-config python3-venv gcc libpython3-dev && \
    python3 -m venv /venv && \
    /venv/bin/pip install --upgrade pip setuptools wheel

# Build the virtualenv as a separate step: Only re-execute this step when requirements.txt changes
FROM build AS build-venv
COPY requirements.txt /requirements.txt
RUN /venv/bin/pip install --disable-pip-version-check -r /requirements.txt

# Copy the virtualenv into a distroless image
FROM gcr.io/distroless/python3-debian11
COPY --from=build-venv /venv /venv
COPY . /controller
WORKDIR /controller
ENTRYPOINT ["/venv/bin/python3", "controller"]
