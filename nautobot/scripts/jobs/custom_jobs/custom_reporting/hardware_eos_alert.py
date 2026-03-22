from datetime import datetime, timedelta
from nautobot.apps.jobs import Job, register_jobs, IntegerVar, StringVar
from nautobot_device_lifecycle_mgmt.models import HardwareLCM
from nautobot.extras.models.secrets import SecretsGroup
from custom_jobs.custom_operations.send_email import SendEmail

name = "Custom Reporting"


class HardwareEOLAlert(Job):

    alert_in_days = IntegerVar(description="Pre-notify before x days", default=90)
    email_recipients = StringVar(description="Comma-separated list of email recipients")
    email_subject = StringVar(
        description="Email subject", default="Hardware End of Support Alert"
    )

    class Meta:
        name = "Hardware End of Support Alert"
        description = "Alert on EoS from Device Lifecycle > Hardware Notices."
        has_sensitive_variables = False

    def run(self, alert_in_days, email_recipients, email_subject):
        current_date = datetime.now().date()
        alert_date = current_date + timedelta(days=alert_in_days)
        alert_list = self.get_alert_list(alert_date)

        if alert_list:
            self.log_alerts(alert_list, alert_in_days)
            email_body = self.construct_email_body(alert_list)
            self.send_email_alert(email_recipients, email_subject, email_body)
        else:
            self.logger.info(
                f"No hardware End of Support detected in the next {alert_in_days} days."
            )

    def get_alert_list(self, alert_date):
        alert_list = []
        hardware_notices = HardwareLCM.objects.all()
        for obj in hardware_notices:
            if obj.end_of_support and obj.end_of_support <= alert_date:
                alert_list.append(obj)
        return alert_list

    def log_alerts(self, alert_list, alert_in_days):
        for alert_obj in alert_list:
            if alert_obj.device_type:
                self.logger.info(
                    f"Device Type: {alert_obj.device_type} - End of support: {alert_obj.end_of_support} in the next {alert_in_days} days.",
                    extra={"object": alert_obj},
                )
            if alert_obj.inventory_item:
                self.logger.info(
                    f"Inventory Item: {alert_obj.inventory_item} - End of support: {alert_obj.end_of_support} in the next {alert_in_days} days.",
                    extra={"object": alert_obj},
                )

    def construct_email_body(self, alert_list):
        hardware_details = "\n".join(
            (
                f"Device Type: {alert_obj.device_type} - End of support: {alert_obj.end_of_support} - URL: https://192.168.31.25{alert_obj.get_absolute_url()}?tab=main"
                if alert_obj.device_type
                else f"Inventory Item: {alert_obj.inventory_item} - End of support: {alert_obj.end_of_support} - URL: https://192.168.31.25/{alert_obj.get_absolute_url()}?tab=main"
            )
            for alert_obj in alert_list
        )

        email_body = (
            "This is an automated email alert from Nautobot. Do not reply.\n\n"
            "Nautobot Job: Hardware End of Support Alert\n\n"
            "The following hardware items are nearing End of Support:\n\n"
            f"{hardware_details}\n\n"
            "Please check the provided URLs for more details.\n\n"
        )
        return email_body

    def send_email_alert(self, email_recipients, email_subject, email_body):
        try:
            credential = SecretsGroup.objects.get(name="BOT_EMAIL")
            email_job = SendEmail()
            email_job.run(
                credential=credential,
                to_email=email_recipients,
                subject=email_subject,
                body=email_body,
            )
            self.logger.info(
                f"Email sent to {email_recipients} regarding hardware End of Support."
            )
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")


register_jobs(HardwareEOLAlert)
