# Monitoring Reference

## Grafana Dashboards

Access Grafana at **http://localhost:3000** (admin credentials in `.env`).

All dashboards are automatically provisioned from `grafana/dashboards/`. No manual import required.

### Network Overview (`network-overview`)

Fleet-wide health status:
- Total device count and online/offline ratio
- Interface utilization summary (top-N by traffic)
- Active alert count by severity
- BGP peer state across all devices

### Device Detail (`device-detail`)

Per-device drill-down (select device from `$device` template variable):
- CPU and memory utilization
- Interface traffic (all interfaces, stacked)
- BGP peer state table
- Recent syslog entries from Loki

### Interface Analytics (`interface-analytics`)

Traffic engineering view:
- In/out bps per interface
- Interface error and drop rates
- CRC errors and packet discards
- Utilization heatmap

### BGP Monitoring (`bgp-monitoring`)

BGP routing health:
- Per-peer state (Established / Idle / Active)
- Received and advertised prefix counts
- Prefix count change rate (alerts if drops >20%)

## Prometheus

Access Prometheus at **http://localhost:9090**.

### Key Metrics

| Metric | Source | Description |
|--------|--------|-------------|
| `ifInOctets`, `ifOutOctets` | Telegraf/SNMP | Interface byte counters (IF-MIB) |
| `ifOperStatus` | Telegraf/SNMP | Interface operational state |
| `bgpPeerState` | Telegraf/SNMP | BGP peer FSM state |
| `bgpPeerFsmEstablishedTransitions` | Telegraf/SNMP | BGP session flap count |
| `node_cpu_seconds_total` | Node Exporter | Host CPU usage |
| `node_memory_MemAvailable_bytes` | Node Exporter | Host memory |
| `probe_success` | Blackbox Exporter | HTTP/ICMP probe success |

### Alert Rules

Defined in `prometheus/alerts/network.yml`:

| Alert | Condition | Severity |
|-------|-----------|----------|
| `DeviceDown` | `up == 0` for 2m | critical |
| `ServiceDown` | `up == 0` for 2m | critical |
| `HighInterfaceUtilization` | `utilization > 80%` for 5m | warning |
| `InterfaceDown` | `ifOperStatus != 1` for 5m | warning |
| `InterfaceHighErrorRate` | `errors/packets > 1%` for 5m | warning |
| `BGPPeerDown` | `bgpPeerState != 6` for 5m | critical |
| `BGPPrefixCountDecreased` | prefix drop > 20% | warning |
| `HighCPU` | CPU > 90% for 10m | warning |
| `HighMemory` | memory > 90% for 10m | warning |
| `DiskSpaceLow` | disk > 85% | warning |

## Alertmanager

Access at **http://localhost:9093**.

Configured in `prometheus/alertmanager.yml`:

- **Slack**: Critical alerts go to `#network-alerts`, warnings to `#network-warnings`.
- **Email**: All critical alerts emailed to `network-ops@example.com`.
- **Inhibition**: If a device is unreachable (`DeviceDown`), suppress sub-resource alerts for that device.
- **Grouping**: Grouped by `alertname` + `device`, 5-minute group_wait.

To enable Slack, set `SLACK_WEBHOOK_URL` in `.env`.

## Loki Log Queries

Access Loki via Grafana's Explore panel or API at **http://localhost:3100**.

### Useful LogQL Queries

```logql
# All logs from a specific device
{job="syslog", device="spine1"}

# BGP state-change events in the last hour
{job="syslog"} |= "BGP" |= "state"

# Interface down events (severity >= error)
{job="syslog", severity=~"error|critical"} |= "moved to down"

# Failed login attempts
{job="syslog"} |= "authentication failure"
```

## Telegraf SNMP Configuration

Telegraf is configured in `telegraf/telegraf.conf`. It polls the following OIDs on all five Containerlab nodes:

**IF-MIB (30s interval):**
- `ifDescr`, `ifType`, `ifMtu`
- `ifInOctets`, `ifOutOctets`, `ifInErrors`, `ifOutErrors`
- `ifOperStatus`, `ifAdminStatus`

**BGP4-MIB (60s interval):**
- `bgpPeerState`, `bgpPeerAdminStatus`
- `bgpPeerInUpdates`, `bgpPeerOutUpdates`
- `bgpPeerFsmEstablishedTime`, `bgpPeerFsmEstablishedTransitions`
