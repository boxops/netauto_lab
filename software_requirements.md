# **INTERMEDIATE NETWORK AUTOMATION STACK**

## **Project Overview**

Create an intermediate-level, production-ready network automation and observability platform that evolves beyond the basic stack. The solution must be containerized, vendor-agnostic, and feature AI-driven network operations capabilities.

**Complexity Level:** Intermediate  
**Target Users:** Network Engineers, NetOps Teams, SREs  
**Deployment Model:** Docker Compose (with Kubernetes migration path consideration)

---

## **1. ARCHITECTURE REQUIREMENTS**

### **1.1 Overall Architecture**
- **Microservices-based** architecture using Docker Compose
- **Multi-network** design: management network, monitoring network, syslog network
- **API-first** approach - all services must expose REST APIs
- **Event-driven** architecture with webhook and message queue support
- **High availability** considerations (health checks, restarts, data persistence)
- **Scalability** design for 100-500 network devices initially

### **1.2 Network Topology**
```
Docker Networks:
├── mgmt-network (172.20.10.0/24) - Management plane
├── monitoring-network (172.20.11.0/24) - Metrics collection
├── syslog-network (172.20.12.0/24) - Syslog ingestion
└── clab (172.20.20.0/24) - Containerlab devices
```

### **1.3 Data Flow Architecture**
- Devices → Telegraf → Prometheus → Grafana (metrics)
- Devices → Syslog Collector → Loki/Elasticsearch (logs)
- Devices → Nautobot Golden Config Plugin (configurations)
- Prometheus → Alertmanager → Webhooks/Integrations
- AI Agents ↔ All systems via APIs

---

## **2. CORE STACK COMPONENTS**

### **2.1 Nautobot (Source of Truth & Config Management)**

**Version:** Latest stable v3.x  
**Purpose:** Network Source of Truth, IPAM, DCIM, configuration management

**Required Plugins:**
- **Golden Config** - Configuration backup, compliance, remediation
- **Device Lifecycle** - Hardware/software lifecycle management
- **ChatOps** - Slack/Mattermost integration
- **BGP Models** - BGP topology and session management
- **Data Validation Engine** - Custom data validation rules
- **Capacity Metrics** - Interface utilization and capacity planning
- **Firewall Models** - ACL and firewall rule management

**Required Configuration:**
- PostgreSQL 15+ database
- Redis for caching and task queue
- Celery workers for background jobs
- Git integration for config version control
- Webhook support to external systems
- Custom scripts directory for automation
- Jobs framework for network operations
- GraphQL API enabled
- Single Sign-On (SSO) ready (LDAP/SAML prep)

**Data Model Requirements:**
- Sites, regions, rack layouts
- Devices, interfaces, cables, connections
- IP addresses, prefixes, VLANs, VRFs
- Circuits, providers
- Config contexts (device/site-level variables)
- Custom fields for extensibility
- Tags and tenant segregation

**Integration Points:**
- Export dynamic Ansible inventory
- Sync with Containerlab topology
- Push intended configs to devices
- Generate telemetry visualisations for Grafana
- API access for AI agents

### **2.2 TPG Stack (Telegraf, Prometheus, Grafana)**

#### **2.2.1 Telegraf (Data Collection Agent)**

**Version:** Latest 1.x  
**Purpose:** Collect metrics from network devices and systems

**Input Plugins Required:**
- `inputs.snmp` - SNMP polling (v2c, v3)
- `inputs.gnmi` - gNMI telemetry streaming
- `inputs.netflow` - NetFlow/sFlow collection
- `inputs.ping` - ICMP latency monitoring
- `inputs.http_response` - API endpoint monitoring
- `inputs.syslog` - Syslog metric extraction (integration with syslog collector)
- `inputs.prometheus` - Scrape other Prometheus exporters

**Output Plugins:**
- `outputs.prometheus_client` - Expose metrics for Prometheus scraping
- `outputs.influxdb_v2` - Optional time-series database

**Configuration Template:**
- 30-second collection interval for interface metrics
- 5-minute interval for system metrics
- Device templates per vendor (Cisco, Arista, Juniper)
- SNMP MIB mappings for common network metrics
- Tag enrichment from Nautobot API

**Key Metrics to Collect:**
- Interface: bytes/packets in/out, errors, discards, utilization %
- CPU, memory, temperature
- BGP peer states, prefix counts
- OSPF neighbor states
- Device availability/uptime
- Response time/latency
- Transceiver optical power levels

#### **2.2.2 Prometheus (Metrics Storage & Alerting)**

**Version:** Latest 3.x  
**Purpose:** Time-series metrics storage and alerting engine

**Required Configuration:**
- **Retention:** 30 days minimum
- **Scrape intervals:** 30s for devices, 15s for critical services
- **Service discovery:** Dynamic targets from Nautobot API
- **Recording rules:** Pre-computed aggregations for dashboards
- **Alert rules:** Network-specific alerting conditions
- **Remote write:** Optional to long-term storage

**Alerting Rules Required:**
- Device down (no metrics for 2 minutes)
- Interface down (operational status changed)
- High interface utilization (>80% for 5 minutes)
- BGP peer down
- CPU/memory threshold violations (>90% for 10 minutes)
- High error rates (>1% for 5 minutes)
- Certificate expiration warnings
- Config drift detected (integration with Nautobot)

**Exporters to Include:**
- node-exporter (for monitoring host system)
- blackbox-exporter (for probing HTTP/TCP/ICMP)
- snmp-exporter (alternative/complement to Telegraf)

#### **2.2.3 Grafana (Visualization & Analytics)**

**Version:** Latest 12.x  
**Purpose:** Metrics visualization, dashboards, and analytics

**Data Sources:**
- Prometheus (metrics)
- Loki (logs) - see syslog section
- Nautobot (via GraphQL/REST)
- PostgreSQL (direct database queries if needed)

**Required Plugins:**
- FlowCharting - Network topology diagrams
- Worldmap Panel - Geographic device mapping
- Status Panel - Service health overview
- Pie Chart - Distribution visualizations

**Pre-built Dashboards Required:**
1. **Network Overview** - Fleet-wide health and statistics
2. **Device Detail** - Per-device deep dive (CPU, memory, interfaces)
3. **Interface Analytics** - Traffic patterns, top talkers, utilization trends
4. **BGP Monitoring** - Peer states, prefix counts, flap detection
5. **Alerting Status** - Active alerts, alert history
6. **Environmental** - Temperature, power, fan status
7. **Capacity Planning** - Growth trends, forecast models
8. **NetFlow Analytics** - Top applications, conversations, protocols (if NetFlow enabled)
9. **Config Compliance** - Compliance status from Nautobot Golden Config
10. **SLA Monitoring** - Latency, jitter, packet loss dashboards

**Additional Features:**
- Annotation support (link changes to events in Nautobot)
- Alert notification channels (Slack, email, PagerDuty, webhook)
- Dashboard variables for site/device filtering
- Templated dashboards for device types
- User authentication and RBAC
- Dashboard versioning
- Snapshot sharing capability

### **2.3 Syslog Collector & Log Management**

**Solution Options** (agent should choose one and justify):

#### Promtail + Loki (Recommended for TPG Stack Integration)**
- Promtail as syslog receiver
- Loki for log storage and querying
- Native Grafana integration
- LogQL query language
- Label-based indexing

**Requirements:**
- Support RFC3164 and RFC5424 syslog formats
- UDP/514, TCP/514, TCP/6514 (TLS) listeners
- Log parsing and field extraction
- Device hostname/IP correlation with Nautobot
- Severity-based filtering and routing
- Log retention: 90 days minimum
- Search and filtering capabilities
- Pattern detection and anomaly identification
- Integration with alerting (send critical syslogs to Prometheus Alertmanager)

**Log Use Cases:**
- AAA authentication events
- Configuration changes
- Interface state changes
- Routing protocol events
- Security events (ACL hits)
- System errors and warnings

**Integration:**
- Parse device info and enrich with Nautobot data (site, role, tags)
- Extract metrics from logs (failed login attempts, config changes/day)
- Trigger webhooks to Nautobot on specific events
- Link from Grafana to relevant logs for time period

---

## **3. ANSIBLE AUTOMATION FRAMEWORK**

### **3.1 Ansible Container Setup**

**Base Image:** Python 3.11+ with Ansible Core 2.15+

**Required Collections:**
- `ansible.netcommon`
- `ansible.utils`
- `arista.eos`
- `cisco.ios`
- `cisco.iosxr`
- `cisco.nxos`
- `juniper.junos`
- `community.general`
- `nautobot.nautobot`

### **3.2 Dynamic Inventory**

**Sources:**
- Nautobot inventory plugin (primary)
- Static inventory for lab devices
- Inventory variables from Nautobot config contexts

**Requirements:**
- Group devices by: site, role, platform, manufacturer, tags
- Automatic credential management (Nautobot secrets integration)
- Inventory caching for performance

### **3.3 Playbook Library**

**Network Operations:**
- `deploy_config.yml` - Push configurations from Nautobot Golden Config
- `backup_config.yml` - Backup all device configs to Nautobot
- `compliance_check.yml` - Run compliance checks (Golden Config)
- `remediate.yml` - Auto-remediation for drift
- `upgrade_firmware.yml` - Software upgrade orchestration with pre/post checks
- `provision_device.yml` - Zero-touch provisioning workflow
- `bounce_interface.yml` - Safely bounce interfaces with pre-checks
- `clear_bgp_session.yml` - Reset BGP sessions with safeguards

**Operational Verification:**
- `health_check.yml` - Multi-vendor health checks (CPU, memory, protocols)
- `connectivity_test.yml` - End-to-end reachability tests
- `validate_bgp.yml` - BGP state validation
- `validate_routing.yml` - Routing table verification
- `check_hardware.yml` - Hardware component status

**Reporting & Auditing:**
- `generate_inventory_report.yml` - Export device inventory to CSV/JSON
- `collect_facts.yml` - Gather device facts and sync to Nautobot
- `audit_config.yml` - Configuration audit against baselines
- `document_topology.yml` - Auto-generate topology documentation

**Disaster Recovery:**
- `full_backup.yml` - Comprehensive backup of all configs
- `restore_device.yml` - Restore device from backup

### **3.4 Ansible Roles**

**Required Roles:**
- `common` - Common tasks for all devices (NTP, SNMP, syslog, banners)
- `interfaces` - Interface configuration management
- `routing` - Routing protocol configuration (BGP, OSPF, static)
- `security` - ACLs, AAA, SSH hardening
- `monitoring` - Configure monitoring agents (SNMP, streaming telemetry)
- `qos` - Quality of Service policies
- `backup_and_restore` - Device backup/restore logic

### **3.5 Ansible Tower/AWX Consideration**

**Optional but Recommended:**
- Provide setup for Ansible AWX (open-source Tower) in docker-compose
- Job templates for common playbooks
- Surveys for user-driven automation
- RBAC and credential management
- Scheduled jobs (nightly backups, compliance checks)
- REST API for AI agent integration
- Job history and audit trail

---

## **4. AI OPS & NETWORK ENGINEERING AGENTS**

### **4.1 AI Agent Framework**

**Architecture:**
- LangChain or LlamaIndex based framework
- OpenAI API support (GPT-4) with fallback to local models (Ollama)
- LangSmith or LangFuse for observability
- Agent memory (conversation history, learned context)
- Tool/function calling capabilities
- RAG (Retrieval Augmented Generation) for network documentation

### **4.2 Network Operations Agent**

**Purpose:** Autonomous monitoring and incident response

**Capabilities:**
- Monitor Prometheus alerts and investigate root causes
- Query logs in Loki/Elasticsearch for error patterns
- Correlate events across systems (metrics + logs + config changes)
- Identify affected services/circuits from device failures
- Generate incident reports with timeline and impact analysis
- Suggest remediation actions based on runbooks
- Execute approved remediations via Ansible (with confirmation)

**Tools/Functions:**
- `query_prometheus(query)` - Run PromQL queries
- `query_logs(device, timerange, pattern)` - Search logs
- `get_device_info(device_name)` - Get device details from Nautobot
- `get_connected_devices(device_name)` - Topology traversal
- `run_ansible_playbook(playbook, devices, check_mode)` - Execute automation
- `get_alert_details(alert_id)` - Get alert context
- `create_incident(title, description, affected_devices)` - Create ticket

**Example Use Cases:**
- "Why is router1 showing high CPU?"
- "What changed in the last hour on devices in site-nyc?"
- "Investigate BGP peer down alert for router2"
- "Generate a network health report for the past week"

### **4.3 Network Engineering Agent**

**Purpose:** Assist with design, implementation, and documentation

**Capabilities:**
- Design network configurations based on requirements
- Generate Ansible playbooks from natural language
- Create device configurations (validated against vendor syntax)
- IP address planning (query Nautobot for available IPs)
- VLAN planning and assignment
- Answer questions about current network state
- Generate topology diagrams (Mermaid or Graphviz)
- Document as-built configurations
- Provide configuration syntax help and examples
- Review configurations for best practices and security issues

**Tools/Functions:**
- `search_nautobot(query)` - Semantic search in Nautobot
- `get_available_ips(prefix, count)` - IP allocation
- `validate_config(device_type, config_snippet)` - Syntax validation
- `generate_config_template(device_role, requirements)` - Template generation
- `get_topology(site)` - Get topology data
- `query_documentation(topic)` - RAG from network docs
- `update_nautobot(object_type, changes)` - Modify Nautobot objects
- `preview_change_impact(device, config_change)` - Impact analysis

**Example Use Cases:**
- "Design BGP configuration for new router in site-sfo"
- "Show me all devices with software version older than 4.28"
- "Generate a playbook to configure VLANs 100-110 on all switches"
- "What is the current IP allocation in the 10.10.0.0/16 prefix?"
- "Create documentation for the OSPF design"

### **4.4 Agent User Interfaces**

**Chat Interface Options:**
1. **Web UI** - Gradio or Streamlit based chat interface
2. **Slack Bot** - Slack app integration via ChatOps
3. **CLI Tool** - Command-line interface for agent interaction
4. **API Service** - REST API for programmatic access

**Requirements:**
- Support multi-turn conversations
- Show agent reasoning steps (thoughts/actions)
- Display data in formatted tables/JSON
- Render diagrams and graphs
- Support file uploads (configs, logs for analysis)
- Authentication and authorization
- Conversation history persistence
- Ability to approve/reject agent actions before execution

### **4.5 Agent Safety and Guardrails**

**Critical Requirements:**
- Read-only mode by default
- Explicit user approval for write operations (config changes, device actions)
- Dry-run/check mode for Ansible execution before actual run
- Audit log of all agent actions
- Rate limiting on device interactions
- Rollback plans for configuration changes
- Restricted access to critical devices/production environments
- Input validation to prevent prompt injection
- Secrets management (never expose credentials in responses)

---

## **5. ADDITIONAL TOOLS & SYSTEMS**

### **5.1 Git Server (Configuration Version Control)**

**Solution:** Gitea or GitLab CE

**Purpose:**
- Version control for Nautobot Golden Config backups
- Store Ansible playbooks, roles, inventory
- Track configuration changes over time
- Pull request workflow for config changes
- Integration with Nautobot Golden Config plugin

**Features:**
- Automated commits from Nautobot on config changes
- Diff visualization
- Webhook triggers on commits
- Repository per device or per site (configurable)

### **5.2 Message Queue (Optional but Recommended)**

**Solution:** RabbitMQ or Redis Streams

**Purpose:**
- Event bus for inter-service communication
- Decouple services via pub/sub
- Queue tasks for asynchronous processing
- WebSocket support for real-time updates

**Use Cases:**
- Nautobot events → trigger Ansible playbooks
- Alert events → notify AI agents
- Config changes → update Git
- Device status → update dashboards

### **5.3 API Gateway (Optional for Advanced Deployments)**

**Solution:** Kong or Traefik

**Purpose:**
- Single entry point for all APIs
- Authentication and authorization
- Rate limiting and throttling
- API analytics
- Load balancing

### **5.4 NetFlow/sFlow Collector (Optional)**

**Solution:** GoFlow2 or nfsen

**Purpose:**
- Collect and analyze network flow data
- Integration with Grafana for visualization
- Top talkers, application visibility

### **5.5 Network Documentation Generator**

**Solution:** Nornir + Markdown/MkDocs

**Purpose:**
- Auto-generate network documentation from device state
- Create topology diagrams
- Build searchable documentation site

**Features:**
- Schedule documentation refreshes
- Export to PDF/HTML
- Version controlled in Git

---

## **6. NETWORK LAB ENVIRONMENT**

### **6.1 Containerlab Topology**

**Topology Design:**
- **Minimum:** 5-node topology (2 spines, 3 leaves) for Clos fabric
- **Device Types:** Mix of Arista cEOS, Cisco cEOS-lab, Nokia SR Linux
- **Protocols:** BGP (eBGP/iBGP), OSPF, VXLAN/EVPN
- **Services:** L2 VPN, L3 VPN, Anycast Gateway

**Topology File:**
- YAML-based Containerlab definition
- Automated startup configs
- Management network auto-configuration
- Simulated client nodes (Linux containers) for testing

**Integration:**
- Automatic registration in Nautobot on deployment
- Pre-configured SNMP, syslog, streaming telemetry
- Ansible inventory sync script
- Access via SSH and console

### **6.2 Traffic Generation**

**Tools:**
- iperf3 containers for bandwidth testing
- ping/mtr for latency monitoring
- Scapy for custom packet generation

---

## **7. INFRASTRUCTURE & ORCHESTRATION**

### **7.1 Docker Compose**

**Requirements:**
- Docker Compose v2.20+
- All services defined in docker-compose.yml
- Environment variables in .env file (with .env.example template)
- Multi-stage builds where applicable for image optimization
- Health checks for all services
- Proper dependency ordering (depends_on with conditions)
- Named volumes for data persistence
- Restart policies (unless-stopped)
- Resource limits (memory, CPU) for critical services
- Logging drivers configured (json-file with rotation)

**Service Organization:**
```
services:
  # Source of Truth
  - nautobot (+ postgres, redis, celery workers)
  
  # Monitoring & Observability
  - prometheus
  - grafana
  - telegraf
  - alertmanager
  - node-exporter
  - blackbox-exporter
  
  # Logging
  - promtail (or rsyslog)
  - loki (or elasticsearch + kibana)
  
  # Automation
  - ansible
  - awx (optional)
  
  # Version Control
  - gitea
  
  # AI Agents
  - ai-ops-agent
  - network-eng-agent
  - agent-ui (web interface)
  
  # Supporting Services
  - rabbitmq (optional)
  - redis (shared cache)
```

### **7.2 Makefile**

**Essential Targets:**
```makefile
init           - Initialize environment
start          - Start all services
stop           - Stop all services
restart        - Restart services
logs           - Tail logs
status         - Check service health
clean          - Remove all data (with confirmation)
backup-data    - Backup all persistent data
restore-data   - Restore from backup
deploy-lab     - Deploy Containerlab topology
destroy-lab    - Destroy Containerlab topology
ansible-shell  - Open Ansible container shell
sync-inventory - Sync lab devices to Nautobot
run-playbook   - Run Ansible playbook (interactive)
agent-chat     - Start AI agent CLI
update         - Pull latest images and restart
health-check   - Run comprehensive health check
```

### **7.3 Initialization & Setup**

**Setup Script (`setup.sh`):**
```bash
# Automated setup script should:
1. Check prerequisites (Docker, Containerlab, Python version)
2. Create .env from template
3. Generate secrets (passwords, API tokens, secret keys)
4. Create Docker networks
5. Pull Docker images
6. Start core services
7. Wait for services to be healthy
8. Initialize Nautobot (run migrations, create superuser)
9. Load Nautobot initial data (sites, device roles, platforms)
10. Configure Prometheus targets
11. Import Grafana dashboards
12. Display access information and next steps
```

### **7.4 Health Monitoring**

**Health Check Script (health_check.sh):**
- Verify all containers are running
- Check service health endpoints
- Test database connectivity
- Verify API accessibility
- Check disk space for volumes
- Report any issues with remediation suggestions

---

## **8. SECURITY REQUIREMENTS**

### **8.1 Authentication & Authorization**

- Unique credentials for each service (no default passwords in production)
- Secrets management in .env file (not committed to Git)
- RBAC enabled in Nautobot, Grafana, AWX
- API token authentication for service-to-service communication
- Consider HashiCorp Vault integration for advanced secret management

### **8.2 Network Security**

- Isolated Docker networks per function
- No unnecessary port exposure on host
- TLS/SSL for web interfaces (provide Let's Encrypt setup guide)
- Encrypted syslog (TCP/TLS) option
- SNMPv3 templates for devices
- Secure SSH configuration templates

### **8.3 Data Protection**

- Encrypted volumes for sensitive data (consider LUKS)
- Regular automated backups
- Backup encryption
- Data retention policies
- Audit logging for all changes

---

## **9. DOCUMENTATION REQUIREMENTS**

### **9.1 README.md**

- Project overview and features
- Architecture diagram (Mermaid or image)
- Prerequisites and system requirements
- Quick start guide (< 10 minutes to running stack)
- Service URLs and default credentials
- Troubleshooting section

### **9.2 Documentation Site (MkDocs)**

**Sections:**
1. **Getting Started**
   - Installation
   - Configuration
   - First steps

2. **Core Components**
   - Nautobot setup and configuration
   - Prometheus/Grafana setup
   - Syslog collector configuration
   - Ansible playbooks usage

3. **AI Agents**
   - Agent capabilities
   - How to interact with agents
   - Example prompts and workflows
   - Safety and guardrails

4. **Network Operations**
   - Running playbooks
   - Monitoring and alerting
   - Incident response workflows
   - Configuration management

5. **Network Lab**
   - Deploying Containerlab topology
   - Lab scenarios and testing
   - Traffic generation

6. **API Reference**
   - Nautobot API examples
   - Prometheus API queries
   - Agent API usage

7. **Development**
   - Adding custom dashboards
   - Creating custom playbooks
   - Extending AI agents
   - Contributing guide

8. **Administration**
   - Backup and restore
   - Upgrading services
   - Performance tuning
   - Security hardening

### **9.3 Code Documentation**

- Inline comments in playbooks
- README in each directory explaining contents
- Jinja2 template documentation
- Agent function/tool docstrings
- API documentation (OpenAPI/Swagger)

---

## **10. TESTING REQUIREMENTS**

### **10.1 Automated Tests**

**Infrastructure Tests:**
- All services start successfully
- Health checks pass
- Inter-service connectivity works
- APIs are accessible

**Ansible Tests:**
- Playbook syntax validation (ansible-lint)
- Dry-run execution against lab devices
- Idempotency tests (run playbook twice, no changes second time)

**Agent Tests:**
- Unit tests for agent tools/functions
- Integration tests with mock APIs
- Example conversation tests
- Safety guardrail tests (reject dangerous operations)

### **10.2 Test Lab Scenarios**

**Scenario Scripts:**
1. Deploy lab → Add to Nautobot → Configure monitoring → Run health check
2. Introduce device failure → Agent detects and reports → Remediate
3. Config drift simulation → Compliance check → Auto-remediation
4. Firmware upgrade workflow → Pre-checks → Upgrade → Post-checks → Rollback on failure
5. New device provisioning → ZTP → Validation

---

## **11. DEPLOYMENT & OPERATIONS**

### **11.1 System Requirements**

**Minimum:**
- CPU: 8 cores
- RAM: 32 GB
- Disk: 100 GB SSD
- OS: Ubuntu 22.04 LTS or Debian 12

**Recommended:**
- CPU: 16 cores
- RAM: 64 GB
- Disk: 500 GB NVMe SSD
- OS: Ubuntu 22.04 LTS

### **11.2 Scalability Considerations**

- Telegraf distributed architecture for large device counts
- Prometheus federation for multi-site deployments
- Grafana high-availability setup guide
- Nautobot horizontal scaling with multiple workers

### **11.3 Backup Strategy**

**Automated Backups:**
- Database dumps (Nautobot Postgres, Prometheus data)
- Volume snapshots
- Git repository backups
- Configuration files

**Backup Schedule:**
- Daily incremental backups
- Weekly full backups
- Retain last 30 days

**Restore Procedure:**
- Documented step-by-step restore process
- Tested regularly (quarterly restore drill)

---

## **13. DELIVERABLES**

The AI agent must create a complete, runnable project with:

### **13.1 Repository Structure**
```
.
├── README.md
├── LICENSE
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Makefile
├── setup.sh
├── docs/
│   ├── index.md
│   ├── installation.md
│   ├── architecture.md
│   ├── agents.md
│   └── ...
├── nautobot/
│   ├── configuration/
│   ├── initializers/
│   └── scripts/
├── ansible/
│   ├── ansible.cfg
│   ├── inventory/
│   ├── playbooks/
│   ├── roles/
│   └── collections/requirements.yml
├── prometheus/
│   ├── prometheus.yml
│   ├── alerts/
│   └── recording_rules/
├── grafana/
│   ├── dashboards/
│   ├── datasources/
│   └── provisioning/
├── telegraf/
│   └── telegraf.conf
├── loki/ (or elasticsearch/)
│   └── config.yml
├── promtail/ (or rsyslog/)
│   └── config.yml
├── ai-agents/
│   ├── ops_agent/
│   ├── engineering_agent/
│   ├── shared/
│   ├── ui/
│   └── requirements.txt
├── containerlab/
│   ├── topologies/
│   │   └── spine-leaf.yml
│   └── configs/
├── scripts/
│   ├── health_check.sh
│   ├── backup.sh
│   ├── sync_inventory.py
│   └── ...
└── tests/
    ├── test_infrastructure.py
    ├── test_ansible.py
    └── test_agents.py
```

### **13.2 Working Features**

On initial deployment (`make init && make start`):
- All services healthy and accessible
- Nautobot populated with initial data
- Prometheus scraping configured services
- Grafana with pre-loaded dashboards
- Syslog collector receiving logs
- Ansible able to connect to Containerlab devices
- AI agents responsive and functional
- Documentation site generated and viewable

### **13.3 Example Workflows**

Provide complete, tested examples of:
1. Deploying a new device
2. Monitoring device health with AI agent
3. Configuring a service using AI-generated playbook
4. Investigating an alert
5. Backing up and restoring configurations
6. Upgrading device software

---

## **14. QUALITY STANDARDS**

- **Code Quality:** All code linted and formatted (Python: black, YAML: yamllint)
- **Documentation:** Every feature documented
- **Consistency:** Follow naming conventions and file structure patterns
- **Error Handling:** Graceful failures with helpful error messages
- **Logging:** Structured logging with appropriate levels
- **Comments:** Complex logic explained
- **Modularity:** Reusable components, avoid duplication

---

## **15. OPTIONAL ENHANCEMENTS (STRETCH GOALS)**

If time permits or for future iterations:
- Kubernetes Helm charts for cloud deployment
- Terraform modules for infrastructure as code
- CI/CD pipeline (GitHub Actions) for testing and deployment
- Multi-tenancy support
- NetBox to Nautobot migration tool
- Machine learning for anomaly detection
- Synthetic monitoring (synthetic transactions)
- Cost tracking and optimization
- Change request workflow system
- Advanced RBAC with approval workflows

---

## **16. SUCCESS CRITERIA**

The project is successful if:
1. ✅ All services start successfully on a fresh system
2. ✅ Containerlab devices are monitored (metrics + logs)
3. ✅ Grafana dashboards show real-time data
4. ✅ Ansible playbooks execute successfully
5. ✅ Nautobot Golden Config backs up device configs
6. ✅ Alerts trigger correctly and appear in Grafana
7. ✅ AI Ops agent can investigate and explain an alert
8. ✅ Network Engineering agent can generate valid configs
9. ✅ All integrations work (Nautobot ↔ Ansible, Prometheus → Grafana, etc.)
10. ✅ Documentation is complete and accurate

---

## **17. CONSTRAINTS & NOTES**

- **Budget:** Free/open-source software only (except LLM API which user provides)
- **Complexity:** Intermediate level - not enterprise-grade but production-capable
- **Vendor Support:** Multi-vendor (Cisco, Arista, Juniper at minimum)
- **Maintainability:** Simple enough for one network engineer to operate
- **Learning Curve:** Well-documented for users familiar with the basic stack
