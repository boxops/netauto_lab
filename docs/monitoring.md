# Monitoring Reference

## Grafana Dashboards

Access at **http://localhost:3000** — all dashboards auto-provisioned from `grafana/dashboards/`.

| Dashboard | UID | What it shows |
|---|---|---|
| Network Overview | `network-overview` | Fleet health, online/offline ratio, active alerts, BGP peer state |
| Device Detail | `device-detail` | Per-device CPU, memory, interface traffic, BGP table, recent syslogs |
| Interface Analytics | `interface-analytics` | In/out bps, error/drop rates, CRC errors, utilization heatmap |
| BGP Monitoring | `bgp-monitoring` | Per-peer state, received/advertised prefix counts, flap detection |

## Prometheus

Access at **http://localhost:9090**.

### Key metrics

| Metric | Source | Description |
|---|---|---|
| `ifInOctets`, `ifOutOctets` | Telegraf/SNMP | Interface byte counters |
| `ifOperStatus`, `ifAdminStatus` | Telegraf/SNMP | Interface oper and admin state |
| `bgpPeerState` | Telegraf/SNMP | BGP peer FSM state (6 = Established) |
| `bgpPeerFsmEstablishedTransitions` | Telegraf/SNMP | BGP session flap count |
| `probe_success` | Blackbox Exporter | ICMP/HTTP probe |
| `node_cpu_seconds_total` | Node Exporter | Host CPU |

### Alert rules (`prometheus/alerts/network.yml`)

| Alert | Condition | Severity |
|---|---|---|
| `DeviceDown` / `ServiceDown` | `up == 0` for 2 m | critical |
| `BGPPeerDown` | `bgpPeerState != 6` for 5 m | critical |
| `InterfaceDown` | `ifOperStatus != 1` for 5 m | warning |
| `HighInterfaceUtilization` | utilization > 80% for 5 m | warning |
| `InterfaceHighErrorRate` | errors/packets > 1% for 5 m | warning |
| `BGPPrefixCountDecreased` | prefix drop > 20% | warning |
| `HighCPU` / `HighMemory` | > 90% for 10 m | warning |
| `DiskSpaceLow` | disk > 85% | warning |

## Alertmanager

Access at **http://localhost:9093**. Configured in `prometheus/alertmanager.yml`.

- **Slack:** Critical → `#network-alerts`, warnings → `#network-warnings` (set `SLACK_WEBHOOK_URL` in `.env`)
- **Inhibition:** If `DeviceDown`, suppress sub-resource alerts for that device
- **Grouping:** By `alertname + device`, 5-minute group_wait

Alertmanager forwards all warning/critical alerts to the internal `alert-event-receiver:8770` service, which the AI agent's AlertPoller consumes. See [pipeline.md](pipeline.md) for the full closed-loop flow.

## Loki

Access via Grafana Explore or API at **http://localhost:3100**.

```logql
# All logs from a specific device
{job="syslog", device="spine1"}

# BGP state-change events
{job="syslog"} |= "BGP" |= "state"

# Interface down events
{job="syslog", severity=~"error|critical"} |= "moved to down"

# Failed login attempts
{job="syslog"} |= "authentication failure"
```

## Telegraf SNMP

Polls all Containerlab nodes via SNMPv2c:

**IF-MIB (30 s):** `ifDescr`, `ifType`, `ifMtu`, `ifInOctets`, `ifOutOctets`, `ifInErrors`, `ifOutErrors`, `ifOperStatus`, `ifAdminStatus`

**BGP4-MIB (60 s):** `bgpPeerState`, `bgpPeerAdminStatus`, `bgpPeerInUpdates`, `bgpPeerOutUpdates`, `bgpPeerFsmEstablishedTime`, `bgpPeerFsmEstablishedTransitions`
