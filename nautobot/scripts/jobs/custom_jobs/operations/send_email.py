"""
Purpose:
Send emails. User input:
- Credential (email username/password)
- To
- Subject
- Body
"""

# Import Nautobot models
from nautobot.apps.jobs import Job, register_jobs, ObjectVar, StringVar, TextVar
from nautobot.extras.models.secrets import Secret, SecretsGroup
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)

import smtplib
import ssl
from email.message import EmailMessage

name = "Operations"


def get_default_credential():
    try:
        return SecretsGroup.objects.get(name="BOT_EMAIL")
    except SecretsGroup.DoesNotExist:
        return None


class SendEmail(Job):
    credential = ObjectVar(
        model=SecretsGroup,
        description="SecretsGroup email address and apppassword",
        default=get_default_credential(),
    )
    to_email = StringVar()
    subject = StringVar()
    body = TextVar()

    class Meta:
        name = "Send Email"
        description = "Supported SMTP servers: Gmail"
        has_sensitive_variables = False

    def run(self, credential, to_email, subject, body):

        from_email = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        )
        from_password = credential.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        )

        em = EmailMessage()
        em["From"] = from_email
        em["To"] = to_email
        em["Subject"] = subject
        em.set_content(body)
        context = ssl.create_default_context()

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
                smtp.login(from_email, from_password)
                smtp.sendmail(from_email, to_email.split(","), em.as_string())
            self.logger.info("Email sent successfully")
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")


register_jobs(SendEmail)
