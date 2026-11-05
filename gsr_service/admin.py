from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from .models import GoodsServiceReceipt, GSRLineItem, GSRAttachment


class GSRLineItemInline(TabularInline):
	model = GSRLineItem
	extra = 0
	readonly_fields = ['purchase_order_line_item', 'quantity_confirmed']
	can_delete = False


class GSRAttachmentInline(TabularInline):
	model = GSRAttachment
	extra = 0
	readonly_fields = ['file', 'original_name', 'uploaded_by', 'uploaded_at']
	can_delete = False


@admin.register(GoodsServiceReceipt)
class GoodsServiceReceiptAdmin(ModelAdmin):
	list_display = ['gsr_number', 'purchase_order', 'store', 'status', 'created_by', 'created']
	list_filter = ['status', 'store']
	search_fields = ['gsr_number', 'purchase_order__po_id', 'store__store_name', 'byd_document_id']
	readonly_fields = ['gsr_number', 'purchase_order', 'store', 'created_by', 'created']
	inlines = [GSRLineItemInline, GSRAttachmentInline]
