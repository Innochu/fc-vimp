from rest_framework import serializers

from .models import GoodsServiceReceipt, GSRLineItem, GSRAttachment


class GSRLineItemSerializer(serializers.ModelSerializer):
	itemObjectID = serializers.CharField(source='purchase_order_line_item.object_id', read_only=True)
	product_id = serializers.CharField(source='purchase_order_line_item.product_id', read_only=True)
	product_name = serializers.CharField(source='purchase_order_line_item.product_name', read_only=True)
	unit_of_measurement = serializers.CharField(source='purchase_order_line_item.unit_of_measurement', read_only=True)
	unit_price = serializers.DecimalField(
		source='purchase_order_line_item.unit_price', max_digits=15, decimal_places=3, read_only=True
	)
	net_value = serializers.DecimalField(max_digits=18, decimal_places=3, read_only=True)

	class Meta:
		model = GSRLineItem
		fields = ['id', 'itemObjectID', 'product_id', 'product_name', 'unit_of_measurement',
				  'unit_price', 'quantity_confirmed', 'net_value']


class GSRAttachmentSerializer(serializers.ModelSerializer):
	url = serializers.SerializerMethodField()

	def get_url(self, obj):
		request = self.context.get('request')
		return request.build_absolute_uri(obj.file.url) if request else obj.file.url

	class Meta:
		model = GSRAttachment
		fields = ['id', 'original_name', 'url', 'uploaded_at']


class GoodsServiceReceiptSerializer(serializers.ModelSerializer):
	po_id = serializers.IntegerField(source='purchase_order.po_id', read_only=True)
	vendor = serializers.CharField(source='purchase_order.vendor.byd_internal_id', read_only=True)
	store_name = serializers.CharField(source='store.store_name', read_only=True)
	store_code = serializers.CharField(source='store.byd_cost_center_code', read_only=True)
	created_by_name = serializers.SerializerMethodField()
	status_text = serializers.CharField(source='get_status_display', read_only=True)
	total_net_value = serializers.DecimalField(max_digits=18, decimal_places=3, read_only=True)
	line_items = GSRLineItemSerializer(many=True, read_only=True)
	attachments = GSRAttachmentSerializer(many=True, read_only=True)

	def get_created_by_name(self, obj):
		user = obj.created_by
		return f"{user.first_name} {user.last_name}".strip() or user.email

	class Meta:
		model = GoodsServiceReceipt
		fields = ['id', 'gsr_number', 'po_id', 'vendor', 'store_name', 'store_code', 'status', 'status_text',
				  'status_reason', 'completion_notes', 'created', 'created_by_name', 'byd_document_id',
				  'total_net_value', 'line_items', 'attachments']
