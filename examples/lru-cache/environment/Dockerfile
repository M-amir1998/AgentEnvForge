# Sandbox image for the ledger task.
#
# The base image is pinned by digest and pytest by version so that every run of this
# task uses byte-identical tooling. Commands run as the numeric user from task.yaml
# (1000:1000); no user account is needed in the image.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install "pytest==8.3.5"
