# Build for Anthropic's OSS Scanner (https://github.com/anthropics/oss-scanner).
#
# The scanner builds this file with the repository as the build context and with
# network access, then audits the finished image with NO network. Anything an
# analysis or a reproducer needs must be installed here. What to look at, and how
# findings are rated, is in .oss-scanner/threat_model.md next to this file.
#
# Python 3.14 because the newest Home Assistant needs it (2026.10.0: >=3.14.2).
# CI tests on 3.12; the repository's tests pass on both.
FROM python:3.14-slim-trixie

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

# 1. The repository's own test environment: the packages .github/workflows/lint.yml
#    installs (a source contract keeps the two lists in step), plus ruff, so a
#    proposed patch can be checked against the lint gate as well. These tests stub
#    Home Assistant and need no network: `python -m pytest tests/` from /src.
RUN pip install pytest pytest-asyncio pytest-cov aiohttp voluptuous ruff

# 2. A real Home Assistant, in its own venv so that its exact pins cannot change
#    what the repository's tests run against. pytest-homeassistant-custom-component
#    pins one Home Assistant release and provides the `hass`, `socket_enabled` and
#    `enable_custom_integrations` fixtures; .oss-scanner/test_ha_harness.py uses
#    them to load this integration into a real core against a fake cloud. Best
#    effort: the tests above do not need it, so a failed install warns instead of
#    failing the build.
RUN python -m venv /opt/ha \
    && (/opt/ha/bin/pip install pytest-homeassistant-custom-component \
        || echo "WARNING: Home Assistant could not be installed into /opt/ha")

# Scanner contract: the checkout lives inside the image, at /src.
COPY . /src
WORKDIR /src

# Run the cheap checks so a broken image shows up in the build log, without letting
# a failure abort the build. No cache directory, so /src stays a clean checkout.
RUN python -m pytest tests/ -q -p no:cacheprovider \
    || echo "WARNING: the repository's tests reported failures"
RUN /opt/ha/bin/python -m pytest .oss-scanner/test_ha_harness.py -q -p no:cacheprovider \
    || echo "WARNING: the Home Assistant harness in /opt/ha does not load hoval_connect"
