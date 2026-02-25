FROM telegraf:latest

# Install SNMP MIBs required for network device monitoring.
# snmp-mibs-downloader is in non-free on Debian Bookworm.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        libsnmp-base \
        snmp && \
    sed -i '/^Components:/s/$/ non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    echo "deb http://deb.debian.org/debian bookworm non-free non-free-firmware" >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends snmp-mibs-downloader && \
    download-mibs && \
    rm -rf /var/lib/apt/lists/*

# Enable loading all installed MIBs (Debian disables them by default)
RUN sed -i '/^mibs/d' /etc/snmp/snmp.conf 2>/dev/null; echo "mibs +ALL" >> /etc/snmp/snmp.conf
