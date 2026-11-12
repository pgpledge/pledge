from django.apps import AppConfig


class PledgeConfig(AppConfig):
    name = "django_pledge"
    label = "pledge"
    verbose_name = "Pledge ledger"
    default_auto_field = "django.db.models.BigAutoField"
