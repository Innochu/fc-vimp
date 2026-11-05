from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction, IntegrityError
from django.db.models import Sum

from egrn_service.models import PurchaseOrder, PurchaseOrderLineItem, Store


# Statuses that still count against the quantity of a PO line.
ACTIVE_STATUSES = ('submitted', 'posted', 'failed')


class GoodsServiceReceipt(models.Model):
	'''
		Confirmation by a store that a service on a Purchase Order (for example a
		maintenance job) has been carried out. Unlike a GRN it carries no stock,
		so nothing is posted to ICG.
	'''
	STATUS_CHOICES = [
		('submitted', 'Submitted'),   # Saved in eGRN, not yet sent to ByD.
		('posted', 'Posted to ByD'),  # Confirmation recorded in ByD.
		('failed', 'ByD Posting Failed'),
		('cancelled', 'Cancelled'),
	]

	purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='service_receipts')
	store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name='service_receipts')
	gsr_number = models.BigIntegerField(unique=True)
	status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='submitted')
	completion_notes = models.TextField(blank=True, default='')
	created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='service_receipts')
	created = models.DateTimeField(auto_now_add=True)
	byd_object_id = models.CharField(max_length=255, blank=True, null=True)
	byd_document_id = models.CharField(max_length=255, blank=True, null=True)
	byd_metadata = models.JSONField(default=dict, blank=True)
	status_reason = models.CharField(max_length=500, blank=True, default='')

	@property
	def total_net_value(self):
		return sum((item.net_value for item in self.line_items.all()), Decimal('0'))

	@classmethod
	def create_receipt(cls, purchase_order, store, user, lines, notes=''):
		'''
			Create a receipt and its lines atomically.
			`lines` is a list of (PurchaseOrderLineItem, quantity) tuples.
		'''
		if not lines:
			raise ValidationError("At least one line item is required.")

		with transaction.atomic():
			receipt = cls(
				purchase_order=purchase_order, store=store, created_by=user,
				completion_notes=notes or '',
			)
			base = int(f"{purchase_order.po_id}1")
			receipt.gsr_number = base
			# Progressively increment the number until it is unique, as GRNs do.
			while True:
				try:
					with transaction.atomic():
						receipt.save()
					break
				except IntegrityError:
					receipt.gsr_number += 1
			for line_item, quantity in lines:
				GSRLineItem(gsr=receipt, purchase_order_line_item=line_item, quantity_confirmed=quantity).save()
		return receipt

	def __str__(self):
		return f"GSR #{self.gsr_number}"

	class Meta:
		verbose_name = 'Goods & Services Receipt'
		verbose_name_plural = 'Goods & Services Receipts'
		ordering = ['-id']
		permissions = [
			('confirm_service', 'Can confirm completion of services (store manager).'),
		]


class GSRLineItem(models.Model):
	gsr = models.ForeignKey(GoodsServiceReceipt, on_delete=models.CASCADE, related_name='line_items')
	purchase_order_line_item = models.ForeignKey(
		PurchaseOrderLineItem, on_delete=models.PROTECT, related_name='gsr_line_items'
	)
	quantity_confirmed = models.DecimalField(max_digits=15, decimal_places=3)

	@property
	def net_value(self):
		return self.quantity_confirmed * self.purchase_order_line_item.unit_price

	@staticmethod
	def confirmed_quantity_for(po_line_item, exclude_id=None):
		qs = GSRLineItem.objects.filter(
			purchase_order_line_item=po_line_item, gsr__status__in=ACTIVE_STATUSES
		)
		if exclude_id:
			qs = qs.exclude(id=exclude_id)
		return qs.aggregate(total=Sum('quantity_confirmed'))['total'] or Decimal('0')

	@classmethod
	def outstanding_quantity(cls, po_line_item):
		return po_line_item.quantity - cls.confirmed_quantity_for(po_line_item)

	def clean(self):
		if self.quantity_confirmed is None or self.quantity_confirmed <= 0:
			raise ValidationError("Quantity confirmed must be greater than 0.")
		line = self.purchase_order_line_item
		if line.purchase_order_id != self.gsr.purchase_order_id:
			raise ValidationError(f"Line {line.object_id} does not belong to PO {self.gsr.purchase_order.po_id}.")
		outstanding = line.quantity - self.confirmed_quantity_for(line, exclude_id=self.id)
		if self.quantity_confirmed > outstanding:
			raise ValidationError(
				f"Quantity confirmed ({self.quantity_confirmed}) is greater than the outstanding "
				f"quantity ({outstanding}) for '{line.product_name}'."
			)

	def save(self, *args, **kwargs):
		self.clean()
		return super().save(*args, **kwargs)

	def __str__(self):
		return f"{self.gsr}: {self.purchase_order_line_item.product_name} ({self.quantity_confirmed})"

	class Meta:
		verbose_name = 'GSR Line Item'
		verbose_name_plural = 'GSR Line Items'


class GSRAttachment(models.Model):
	'''Evidence of the completed job (job sheet, photos, signed report).'''
	gsr = models.ForeignKey(GoodsServiceReceipt, on_delete=models.CASCADE, related_name='attachments')
	file = models.FileField(upload_to='gsr_attachments/%Y/%m/')
	original_name = models.CharField(max_length=255, blank=True, default='')
	uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
	uploaded_at = models.DateTimeField(auto_now_add=True)

	def __str__(self):
		return self.original_name or self.file.name

	class Meta:
		verbose_name = 'GSR Attachment'
		verbose_name_plural = 'GSR Attachments'
