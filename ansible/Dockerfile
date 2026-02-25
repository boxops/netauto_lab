FROM python:3.11-slim

LABEL maintainer="netauto-lab"
LABEL description="Ansible automation container with network collections"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    sshpass \
    netcat-openbsd \
    curl \
    iputils-ping \
    jq \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
RUN pip install --no-cache-dir \
    ansible-core==2.17.* \
    ansible-lint \
    pynautobot \
    napalm \
    netmiko \
    nornir \
    nornir-napalm \
    paramiko \
    requests \
    jinja2 \
    pyyaml \
    rich

WORKDIR /ansible

# Install Ansible collections
COPY collections/requirements.yml /ansible/collections/requirements.yml
RUN ansible-galaxy collection install -r /ansible/collections/requirements.yml

# Copy ansible.cfg
COPY ansible.cfg /etc/ansible/ansible.cfg

CMD ["sleep", "infinity"]
