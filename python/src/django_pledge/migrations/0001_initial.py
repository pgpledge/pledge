from django.db import migrations, models

import django_pledge.fields
from django_pledge.json import LedgerJSONEncoder
from django_pledge.operations import PledgeExtension


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        PledgeExtension(),
        migrations.CreateModel(
            name="Account",
            fields=[
                ("id", django_pledge.fields.AccountIdField(primary_key=True, serialize=False)),
                ("name", models.TextField()),
                ("currency", models.TextField()),
                ("balance", django_pledge.fields.NumericField()),
                ("version", models.BigIntegerField()),
                ("allow_negative_balance", models.BooleanField()),
                ("allow_positive_balance", models.BooleanField()),
                ("metadata", models.JSONField(null=True, encoder=LedgerJSONEncoder)),
                ("created_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField()),
                ("closed_at", models.DateTimeField(null=True)),
            ],
            options={
                "db_table": '"pledge"."accounts_view"',
                "ordering": ["id"],
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Entry",
            fields=[
                ("id", django_pledge.fields.EntryIdField(primary_key=True, serialize=False)),
                ("amount", django_pledge.fields.NumericField()),
                ("account_previous_balance", django_pledge.fields.NumericField()),
                ("account_current_balance", django_pledge.fields.NumericField()),
                ("account_version", models.BigIntegerField()),
                ("created_at", models.DateTimeField()),
                ("event_at", models.DateTimeField()),
                ("metadata", models.JSONField(null=True, encoder=LedgerJSONEncoder)),
            ],
            options={
                "verbose_name_plural": "entries",
                "db_table": '"pledge"."entries_view"',
                "ordering": ["account_id", "account_version"],
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="Transfer",
            fields=[
                ("id", django_pledge.fields.TransferIdField(primary_key=True, serialize=False)),
                ("amount", django_pledge.fields.NumericField()),
                ("created_at", models.DateTimeField()),
                ("event_at", models.DateTimeField()),
                ("metadata", models.JSONField(null=True, encoder=LedgerJSONEncoder)),
            ],
            options={
                "db_table": '"pledge"."transfers_view"',
                "ordering": ["id"],
                "managed": False,
            },
        ),
    ]
