FROM alpine:edge AS build
LABEL maintainer="Björn Busse <bj.rn@baerlin.eu>"
LABEL org.opencontainers.image.source=https://github.com/opsboost/iss-display-controller
LABEL org.label-schema.description="iss display controller"
LABEL org.label-schema.name="iss-display-controller"
LABEL org.label-schema.schema-version="1.0"
LABEL org.label-schema.vcs-url="https://github.com/opsboost/iss-display-controller"

# Base build deps for compiling Python wheels if needed
RUN apk add --no-cache \
    git \
    python3 \
    python3-dev \
    py3-pip \
    build-base \
    libxkbcommon-dev \
    pkgconf && \
    python3 -m venv /venv && \
    /venv/bin/pip install --upgrade pip setuptools wheel

# Build the virtualenv as a separate step: Only re-execute this step when requirements.txt changes
FROM build AS build-venv

COPY requirements.txt /requirements.txt
# pip/setuptools/wheel are build-time only; stripped here so the copied venv
# never carries them into the final image's layer history
RUN /venv/bin/pip install --disable-pip-version-check -r /requirements.txt \
    && rm -rf /venv/lib/python3.*/site-packages/pip \
               /venv/lib/python3.*/site-packages/pip-*.dist-info \
               /venv/lib/python3.*/site-packages/setuptools \
               /venv/lib/python3.*/site-packages/setuptools-*.dist-info \
               /venv/lib/python3.*/site-packages/wheel \
               /venv/lib/python3.*/site-packages/wheel-*.dist-info \
               /venv/lib/python3.*/site-packages/pkg_resources \
               /venv/lib/python3.*/site-packages/_distutils_hack \
               /venv/lib/python3.*/site-packages/distutils-precedence.pth \
               /venv/bin/pip*

# Minimal runtime on Alpine; ensure Python runtime libs present
FROM alpine:edge
RUN apk add --no-cache python3
COPY --from=build-venv /venv /venv
COPY . /controller
WORKDIR /controller
ENTRYPOINT ["/venv/bin/python3", "/controller/controller.py"]
