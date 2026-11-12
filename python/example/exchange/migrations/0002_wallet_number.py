from django.db import migrations, models

import exchange.models


def number_existing_wallets(apps, schema_editor):
    Wallet = apps.get_model("exchange", "Wallet")
    taken = set()
    for wallet in Wallet.objects.all():
        while (number := exchange.models.new_account_number()) in taken:
            pass
        taken.add(number)
        wallet.number = number
        wallet.save(update_fields=["number"])


class Migration(migrations.Migration):
    dependencies = [("exchange", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="wallet",
            name="number",
            field=models.CharField(editable=False, max_length=10, null=True),
        ),
        migrations.RunPython(number_existing_wallets, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="wallet",
            name="number",
            field=models.CharField(
                default=exchange.models.new_account_number,
                editable=False,
                max_length=10,
                unique=True,
            ),
        ),
    ]
