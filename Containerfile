# moonshine-python carries CPython on musl with brush as the only shell.
# Both stages use it, so the venv's compiled extension modules are built
# against the same interpreter that ends up running them
ARG BASE_IMAGE=ghcr.io/bbusse/moonshine-python:latest

FROM ${BASE_IMAGE} AS build
LABEL maintainer="Björn Busse <bj.rn@baerlin.eu>"
LABEL org.opencontainers.image.source=https://github.com/opsboost/iss-display-controller
LABEL org.label-schema.description="iss display controller"
LABEL org.label-schema.name="iss-display-controller"
LABEL org.label-schema.schema-version="1.0"
LABEL org.label-schema.vcs-url="https://github.com/opsboost/iss-display-controller"

# Base build deps for compiling Python wheels if needed.
# python3 comes from the base image, and seeds the venv's pip from its own
# bundled ensurepip wheels, so the py3-pip package is not needed here either
RUN apk add --no-cache \
    git \
    python3-dev \
    gcc \
    musl-dev \
    jpeg-dev \
    zlib-dev \
    libwebp-dev \
    libxkbcommon-dev \
    pkgconf && \
    python3 -m venv /venv && \
    /venv/bin/pip install --upgrade pip setuptools wheel

# Build the virtualenv as a separate step: Only re-execute this step when
# requirements.txt or DEPS_REF changes. requirements.txt pins git branches, so
# it stays identical when those branches move; pass the branch heads as
# DEPS_REF to rebuild the venv when a dependency changed
FROM build AS build-venv

ARG DEPS_REF=unpinned
COPY requirements.txt /requirements.txt
# pip/setuptools/wheel are build-time only; stripped here so the copied venv
# never carries them into the final image's layer history
RUN printf '%s\n' "${DEPS_REF}" > /venv/deps-ref \
    && /venv/bin/pip install --disable-pip-version-check --no-binary Pillow -r /requirements.txt \
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

# Selenium Manager resolves a driver and a browser at run time. We pass both
# explicitly and set SE_DISABLE_DRIVER_MANAGEMENT, so all three binaries are
# dead weight, and the mac and windows ones could never run here anyway
RUN rm -rf /venv/lib/python3.*/site-packages/selenium/webdriver/common/linux \
           /venv/lib/python3.*/site-packages/selenium/webdriver/common/macos \
           /venv/lib/python3.*/site-packages/selenium/webdriver/common/windows

# Byte code is regenerated on first import. Keeping it would cost more in image
# size than the one recompile costs at startup.
# Done in python rather than with find, which the base image has no coreutils for
RUN python3 <<'PY'
import pathlib, shutil
venv = pathlib.Path("/venv")
for d in list(venv.rglob("__pycache__")):
    shutil.rmtree(d, ignore_errors=True)
for f in list(venv.rglob("*.pyc")):
    f.unlink(missing_ok=True)
PY

# Minimal runtime; the interpreter is already there, so only the libraries
# our extension modules link against are added
FROM ${BASE_IMAGE}
# xkeyboard-config comes in as a hard dependency of libxkbcommon, so it cannot
# be removed with apk, but its data is only read to compile a keymap from
# layout names. We compile the one the compositor hands us over an fd, so the
# data goes in the same layer that installed it
RUN apk add --no-cache \
    cairo \
    pango \
    libjpeg-turbo \
    zlib \
    libwebp \
    libwebpmux \
    libwebpdemux \
    libxkbcommon && \
    rm -rf /usr/share/X11 /usr/share/xkeyboard-config-2
COPY --from=build-venv /venv /venv
COPY . /controller
WORKDIR /controller
ENTRYPOINT ["/venv/bin/python3", "/controller/controller.py"]
