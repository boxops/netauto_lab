"""
Load Golden Config compliance rules into Nautobot from YAML files.

Rule files live in:  /opt/nautobot/scripts/compliance_rules/<platform>.yml

Run via:
    docker exec netauto-nautobot-worker-1 bash -c \
        "nautobot-server shell < /opt/nautobot/scripts/jobs/custom_jobs/configuration/load_compliance_rules.py"
"""

import glob
import os
import yaml

from nautobot_golden_config.models import ComplianceFeature, ComplianceRule
from nautobot_golden_config.choices import ComplianceRuleConfigTypeChoice
from nautobot.dcim.models import Platform

RULES_DIR = getattr(
    __builtins__, "_COMPLIANCE_RULES_DIR",
    "/opt/nautobot/scripts/compliance_rules",
)
# When running directly (not via nautobot-server shell pipe), resolve relative to this file.
if not os.path.isdir(RULES_DIR):
    RULES_DIR = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "compliance_rules")
    )

CONFIG_TYPE_MAP = {
    "cli":  ComplianceRuleConfigTypeChoice.TYPE_CLI,
    "json": ComplianceRuleConfigTypeChoice.TYPE_JSON,
    "xml":  ComplianceRuleConfigTypeChoice.TYPE_XML,
}


def load_rules():
    rule_files = sorted(glob.glob(os.path.join(RULES_DIR, "*.yml")))
    if not rule_files:
        print(f"No YAML rule files found in {os.path.abspath(RULES_DIR)}")
        return

    created = updated = errors = 0

    for path in rule_files:
        print(f"\nLoading: {os.path.basename(path)}")
        with open(path) as f:
            rules = yaml.safe_load(f) or []

        for rule_def in rules:
            slug   = rule_def["feature_slug"]
            driver = rule_def["platform_network_driver"]

            platform = Platform.objects.filter(network_driver=driver).first()
            if not platform:
                print(f"  ERROR: platform '{driver}' not found — skipping '{slug}'")
                errors += 1
                continue

            feature, feat_created = ComplianceFeature.objects.get_or_create(
                slug=slug,
                defaults={"name": slug.replace("-", " ").title()},
            )
            if feat_created:
                print(f"  Created ComplianceFeature: {slug}")

            config_type = CONFIG_TYPE_MAP.get(rule_def.get("config_type", "cli"))

            rule, rule_created = ComplianceRule.objects.update_or_create(
                feature=feature,
                platform=platform,
                defaults={
                    "config_ordered":     rule_def.get("config_ordered", False),
                    "match_config":       rule_def.get("match_config", ""),
                    "config_type":        config_type,
                    "config_remediation": rule_def.get("config_remediation", False),
                },
            )
            action = "Created" if rule_created else "Updated"
            print(f"  {action} rule: {driver} / {slug}")
            if rule_created:
                created += 1
            else:
                updated += 1

    print(f"\nDone — {created} created, {updated} updated, {errors} errors.")


load_rules()
