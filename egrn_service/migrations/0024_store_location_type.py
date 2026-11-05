from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('egrn_service', '0023_alter_stockconsumptionrecord_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='store',
            name='location_type',
            field=models.CharField(
                choices=[('retail', 'Retail Store'), ('scd', 'SCD Location')],
                default='retail',
                help_text='SCD locations receive vendor-direct supplies from the SCD portal.',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='store',
            name='post_to_icg',
            field=models.BooleanField(
                default=True,
                help_text="Set to No only if this location isn't tracked in ICG. "
                          "Goods received here will then not be posted to ICG.",
            ),
        ),
    ]
