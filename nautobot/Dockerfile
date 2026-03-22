FROM networktocode/nautobot:latest-py3.11

USER root

# Install tshark (required by pyshark for packet capture jobs)
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tshark && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    nautobot-golden-config \
    nautobot-device-lifecycle-mgmt \
    nautobot-ssot \
    nornir-nautobot \
    ncclient \
    deepdiff \
    pyshark

USER nautobot
