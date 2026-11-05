from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('egrn_service', '0023_alter_stockconsumptionrecord_options'),
    ]

    operations = [
        migrations.CreateModel(
            name='GoodsServiceReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('gsr_number', models.BigIntegerField(unique=True)),
                ('status', models.CharField(
                    choices=[('submitted', 'Submitted'), ('posted', 'Posted to ByD'),
                             ('failed', 'ByD Posting Failed'), ('cancelled', 'Cancelled')],
                    default='submitted', max_length=10)),
                ('completion_notes', models.TextField(blank=True, default='')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('byd_object_id', models.CharField(blank=True, max_length=255, null=True)),
                ('byd_document_id', models.CharField(blank=True, max_length=255, null=True)),
                ('byd_metadata', models.JSONField(blank=True, default=dict)),
                ('status_reason', models.CharField(blank=True, default='', max_length=500)),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='service_receipts',
                    to=settings.AUTH_USER_MODEL)),
                ('purchase_order', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='service_receipts',
                    to='egrn_service.purchaseorder')),
                ('store', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='service_receipts',
                    to='egrn_service.store')),
            ],
            options={
                'verbose_name': 'Goods & Services Receipt',
                'verbose_name_plural': 'Goods & Services Receipts',
                'ordering': ['-id'],
                'permissions': [('confirm_service', 'Can confirm completion of services (store manager).')],
            },
        ),
        migrations.CreateModel(
            name='GSRLineItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_confirmed', models.DecimalField(decimal_places=3, max_digits=15)),
                ('gsr', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='line_items',
                    to='gsr_service.goodsservicereceipt')),
                ('purchase_order_line_item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='gsr_line_items',
                    to='egrn_service.purchaseorderlineitem')),
            ],
            options={
                'verbose_name': 'GSR Line Item',
                'verbose_name_plural': 'GSR Line Items',
            },
        ),
        migrations.CreateModel(
            name='GSRAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to='gsr_attachments/%Y/%m/')),
                ('original_name', models.CharField(blank=True, default='', max_length=255)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('gsr', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='attachments',
                    to='gsr_service.goodsservicereceipt')),
                ('uploaded_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name='+',
                    to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'GSR Attachment',
                'verbose_name_plural': 'GSR Attachments',
            },
        ),
    ]
