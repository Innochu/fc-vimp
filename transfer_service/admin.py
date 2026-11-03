from django.contrib import admin, messages
from django_q.tasks import async_task
from unfold.admin import ModelAdmin
from .models import (
    InboundDelivery, InboundDeliveryLineItem,
    TransferReceiptNote, TransferReceiptLineItem,
    StoreAuthorization, SourceLocationAuthorization
)


class TransferReceiptLineItemInline(admin.TabularInline):
    model = TransferReceiptLineItem
    extra = 0


@admin.register(TransferReceiptNote)
class TransferReceiptNoteAdmin(ModelAdmin):
    list_display = ('receipt_number', 'inbound_delivery', 'approval_status', 'synced_to_sap', 'byd_hold', 'created_date', 'created_by')
    list_filter = ('approval_status', 'synced_to_sap', 'created_date', 'inbound_delivery__destination_store')
    search_fields = ('receipt_number', 'inbound_delivery__delivery_id', 'inbound_delivery__sales_order_reference')
    readonly_fields = ('receipt_number', 'created_date')
    inlines = [TransferReceiptLineItemInline]
    actions = ['retry_byd_sync']

    @admin.display(description='ByD hold')
    def byd_hold(self, obj):
        held = (obj.metadata or {}).get('byd_goods_issue_held') or {}
        return held.get('reason', '')

    @admin.action(description='Retry ByD sync')
    def retry_byd_sync(self, request, queryset):
        """Re-queue approved, unsynced receipts - e.g. once a ByD hold has been resolved."""
        queued = skipped = 0
        for receipt in queryset:
            if receipt.approval_status != 'approved' or receipt.synced_to_sap:
                skipped += 1
                continue
            async_task('vimp.tasks.sync_approved_receipt_to_sap', receipt.id)
            queued += 1
        if queued:
            self.message_user(request, f"Queued ByD sync for {queued} receipt(s).", messages.SUCCESS)
        if skipped:
            self.message_user(request, f"Skipped {skipped} receipt(s) that are not approved or already synced.", messages.WARNING)


class InboundDeliveryLineItemInline(admin.TabularInline):
    model = InboundDeliveryLineItem
    extra = 0
    readonly_fields = ('object_id', 'product_id', 'product_name', 'quantity_expected', 'quantity_received', 'unit_of_measurement')


@admin.register(InboundDelivery)
class InboundDeliveryAdmin(ModelAdmin):
    list_display = ('delivery_id', 'source_location_name', 'destination_store', 'delivery_date', 'delivery_status_code', 'delivery_type_code', 'is_fully_received')
    list_filter = ('delivery_status_code', 'delivery_type_code', 'delivery_date', 'destination_store', 'source_location_id')
    search_fields = ('delivery_id', 'object_id', 'sales_order_reference', 'source_location_id', 'source_location_name')
    readonly_fields = ('object_id', 'delivery_id', 'delivery_date', 'created_date', 'total_quantity_expected', 'total_quantity_received')
    inlines = [InboundDeliveryLineItemInline]


@admin.register(StoreAuthorization)
class StoreAuthorizationAdmin(ModelAdmin):
    list_display = ('user', 'store', 'role', 'created_date')
    list_filter = ('role', 'created_date', 'store')
    search_fields = ('user__username', 'user__email', 'store__store_name')
    readonly_fields = ('created_date',)


@admin.register(SourceLocationAuthorization)
class SourceLocationAuthorizationAdmin(ModelAdmin):
    list_display = ('user', 'source_location_id', 'source_location_name', 'role', 'created_date')
    list_filter = ('role', 'created_date', 'source_location_id')
    search_fields = ('user__username', 'user__email', 'source_location_id', 'source_location_name')
    readonly_fields = ('source_location_name', 'created_date')
    autocomplete_fields = ('user',)