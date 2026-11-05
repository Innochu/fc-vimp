from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core_service.models import CustomUser, VendorProfile
from egrn_service.models import PurchaseOrder, PurchaseOrderLineItem, Store

from .models import GoodsServiceReceipt, GSRLineItem


class GoodsServiceReceiptTests(TestCase):
	def setUp(self):
		self.user = CustomUser.objects.create_user(
			username="gsr_user", email="store.manager@example.com", password="TestPass123"
		)
		vendor = VendorProfile.objects.create(byd_internal_id="VEND-GSR", byd_metadata={})
		self.store = Store.objects.create(
			store_name="Store One", store_email=self.user.email,
			icg_warehouse_code="S-1", byd_cost_center_code="S-1", metadata={}
		)
		self.po = PurchaseOrder.objects.create(
			vendor=vendor, object_id="PO-GSR", po_id=900, total_net_amount=Decimal('1000'),
			date=timezone.now(), metadata={}
		)
		self.line = PurchaseOrderLineItem.objects.create(
			purchase_order=self.po, delivery_store=self.store, object_id="POLI-GSR",
			product_id="SVC-1", product_name="Generator servicing", quantity=Decimal('4'),
			unit_price=Decimal('250'), unit_of_measurement="EA",
			metadata={'ProductID': 'SVC-1'}
		)

	def _create(self, quantity):
		return GoodsServiceReceipt.create_receipt(
			self.po, self.store, self.user, [(self.line, Decimal(quantity))], notes="Done"
		)

	def test_create_receipt_numbers_and_values(self):
		receipt = self._create('1')
		self.assertEqual(receipt.gsr_number, 9001)
		self.assertEqual(receipt.status, 'submitted')
		self.assertEqual(receipt.total_net_value, Decimal('250'))

	def test_numbers_increment_per_po(self):
		first, second = self._create('1'), self._create('1')
		self.assertEqual((first.gsr_number, second.gsr_number), (9001, 9002))

	def test_cannot_confirm_more_than_outstanding(self):
		self._create('3')
		self.assertEqual(GSRLineItem.outstanding_quantity(self.line), Decimal('1'))
		with self.assertRaises(ValidationError):
			self._create('2')
		# The failed attempt must not leave a receipt behind.
		self.assertEqual(GoodsServiceReceipt.objects.count(), 1)

	def test_cancelled_receipt_frees_quantity(self):
		receipt = self._create('4')
		receipt.status = 'cancelled'
		receipt.save()
		self.assertEqual(GSRLineItem.outstanding_quantity(self.line), Decimal('4'))

	def test_quantity_must_be_positive(self):
		with self.assertRaises(ValidationError):
			self._create('0')

	def test_line_from_another_po_is_rejected(self):
		other_po = PurchaseOrder.objects.create(
			vendor=self.po.vendor, object_id="PO-OTHER", po_id=901,
			total_net_amount=Decimal('10'), date=timezone.now(), metadata={}
		)
		with self.assertRaises(ValidationError):
			GoodsServiceReceipt.create_receipt(other_po, self.store, self.user, [(self.line, Decimal('1'))])
