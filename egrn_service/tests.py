import os
from decimal import Decimal
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from django.conf import settings
from django.test import TestCase, override_settings
from django.db import models
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from core_service.models import CustomUser, VendorProfile
from egrn_service.views import weighted_average, download_grns

from .models import (
	GoodsReceivedNote, GoodsReceivedLineItem,
	PurchaseOrder, PurchaseOrderLineItem,
	StockConsumptionRecord, Store
)


class WeightedAverageConsumptionTest(TestCase):

	def setUp(self):
		self.user = CustomUser.objects.create_user(
			username="wacstest",
			email="wacstest@example.com",
			password="TestPass123"
		)
		self.vendor_profile = VendorProfile.objects.create(
			user=self.user,
			byd_internal_id="VEND-001",
			byd_metadata={}
		)
		self.store = Store.objects.create(
			store_name="Main Warehouse",
			store_email="warehouse@example.com",
			icg_warehouse_name="ICG Main",
			icg_warehouse_code="ICG-0001",
			byd_cost_center_code="4100003-17",
			metadata={}
		)
		self.purchase_order = PurchaseOrder.objects.create(
			vendor=self.vendor_profile,
			object_id="PO-0001",
			po_id=100,
			total_net_amount=Decimal('250'),
			date=timezone.now(),
			metadata={}
		)
		line_item = PurchaseOrderLineItem(
			purchase_order=self.purchase_order,
			delivery_store=self.store,
			object_id="POLI-1",
			product_id="PROD-A",
			product_name="Product A",
			quantity=Decimal('5'),
			unit_price=Decimal('50'),
			unit_of_measurement="KGM",
			metadata={"ProductID": "PROD-A", "QuantityUnitCode": "KGM"}
		)
		models.Model.save(line_item)

		grn = GoodsReceivedNote(
			purchase_order=self.purchase_order,
			grn_number=1234
		)
		models.Model.save(grn)

		GoodsReceivedLineItem.objects.create(
			grn=grn,
			purchase_order_line_item=line_item,
			quantity_received=Decimal('5'),
			net_value_received=Decimal('250'),
			gross_value_received=Decimal('250'),
			metadata={"ProductID": "PROD-A", "QuantityUnitCode": "KGM"}
		)

		StockConsumptionRecord.objects.create(
			product_id="PROD-A",
			product_name="Product A",
			quantity=Decimal('1'),
			unit_cost=Decimal('70'),
			unit_of_measurement="KGM",
			cost_center=self.store.byd_cost_center_code,
			metadata={"reason": "WAC test"}
		)

		self.factory = APIRequestFactory()

	def test_consumption_adjusts_weighted_average(self):
		request = self.factory.get("/api/v1/weighted-average", {"product_id": "PROD-A"})
		request.user = self.user

		response = weighted_average(request)
		self.assertEqual(response.status_code, 200)
		result = response.data["data"]["results"][0]
		self.assertAlmostEqual(result["wac"], 45.0, places=2)
		events = [entry["event"] for entry in result["history"]]
		self.assertIn("consumption", events)


@override_settings(
	CACHES={
		'default': {
			'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
			'LOCATION': 'egrn-service-tests',
		}
	},
	CACHALOT_ENABLED=False,
)
class DownloadGRNsViewTests(TestCase):
	def setUp(self):
		self.factory = APIRequestFactory()
		self.user = CustomUser.objects.create_user(
			username="download_user",
			email="download@example.com",
			password="StrongPass123"
		)
		vendor_user = CustomUser.objects.create_user(
			username="vendor_user",
			email="vendor@example.com",
			password="VendorPass123",
			first_name="Vendor",
			last_name="Owner",
		)
		self.vendor_profile = VendorProfile.objects.create(
			user=vendor_user,
			byd_internal_id="VEND-999",
			byd_metadata={}
		)
		self.store = Store.objects.create(
			store_name="Export Store",
			store_email="store@example.com",
			icg_warehouse_name="WH Export",
			icg_warehouse_code="WH-EXP",
			byd_cost_center_code="4100003-99",
			metadata={}
		)
		self.purchase_order = PurchaseOrder.objects.create(
			vendor=self.vendor_profile,
			object_id="PO-EXPORT",
			po_id=5555,
			total_net_amount=Decimal('1500'),
			date=timezone.now(),
			metadata={}
		)
		self.po_line_item = PurchaseOrderLineItem.objects.create(
			purchase_order=self.purchase_order,
			delivery_store=self.store,
			object_id="PO-EXPORT-LINE",
			product_id="PROD-EXP",
			product_name="Export Product",
			quantity=Decimal('15'),
			unit_price=Decimal('100'),
			unit_of_measurement="EA",
			metadata={'ProductID': 'PROD-EXP'}
		)
		self.grn = GoodsReceivedNote.objects.create(
			purchase_order=self.purchase_order,
			grn_number=7777
		)
		GoodsReceivedLineItem.objects.create(
			grn=self.grn,
			purchase_order_line_item=self.po_line_item,
			quantity_received=Decimal('10'),
			net_value_received=Decimal('1000'),
			gross_value_received=Decimal('1050'),
			metadata={}
		)

	def test_download_grns_generates_file_and_link(self):
		with TemporaryDirectory() as tmp_dir, override_settings(MEDIA_ROOT=tmp_dir):
			request = self.factory.get('/download-grns', {'po_id': self.purchase_order.po_id})
			force_authenticate(request, user=self.user)
			response = download_grns(request)
			
			self.assertEqual(response.status_code, status.HTTP_200_OK)
			response_data = response.data.get('data', {})
			self.assertEqual(response_data.get('row_count'), 1)
			download_url = response_data.get('download_url')
			self.assertIsNotNone(download_url)
			
			parsed = urlparse(download_url)
			media_prefix = '/' + (settings.MEDIA_URL or 'media/').lstrip('/')
			self.assertTrue(parsed.path.startswith(media_prefix))
			
			relative_path = parsed.path[len(media_prefix):]
			file_path = os.path.join(tmp_dir, relative_path.replace('/', os.sep))
			self.assertTrue(os.path.exists(file_path))


class SCDStoreAccessTests(TestCase):
	def setUp(self):
		from transfer_service.models import StoreAuthorization
		self.StoreAuthorization = StoreAuthorization
		self.user = CustomUser.objects.create_user(
			username="scd_user", email="scd.user@example.com", password="TestPass123"
		)
		self.retail = Store.objects.create(
			store_name="Retail A", store_email="retail@example.com",
			icg_warehouse_code="R-1", byd_cost_center_code="R-1", metadata={}
		)
		self.scd = Store.objects.create(
			store_name="SCD Lagos", store_email="scd@example.com",
			icg_warehouse_code="S-1", byd_cost_center_code="S-1",
			location_type=Store.LOCATION_TYPE_SCD, metadata={}
		)

	def test_email_match_still_grants_retail_store(self):
		self.retail.store_email = self.user.email
		self.retail.save()
		from .access import get_user_stores
		self.assertEqual(list(get_user_stores(self.user)), [self.retail])

	def test_store_authorization_grants_scd_only(self):
		from .access import get_user_stores
		self.StoreAuthorization.objects.create(user=self.user, store=self.scd, role='clerk')
		self.StoreAuthorization.objects.create(user=self.user, store=self.retail, role='clerk')
		self.assertEqual(list(get_user_stores(self.user)), [self.scd])

	def test_viewer_role_grants_nothing(self):
		from .access import get_user_stores
		self.StoreAuthorization.objects.create(user=self.user, store=self.scd, role='viewer')
		self.assertEqual(list(get_user_stores(self.user)), [])

	def test_source_location_approver_grants_scd_store(self):
		from transfer_service.models import SourceLocationAuthorization
		from .access import get_user_stores
		SourceLocationAuthorization.objects.create(
			user=self.user, source_location_id="S-1", source_location_name="SCD Lagos"
		)
		self.assertEqual(list(get_user_stores(self.user)), [self.scd])
