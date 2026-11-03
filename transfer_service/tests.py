"""
Tests for the eGRN 2 sales-order receipt sync (SCD approval -> ByD).

A store receives directly against a sales order; on approval eGRN must post the
goods issue from the order's outbound delivery request before the goods receipt
can be booked. ByD only creates that request once the order's customer demand
has been released, which can trail the sales order itself. These tests pin:
  - unreleased sales orders are refused at search and held at sync,
  - a missing delivery request holds the receipt with an actionable reason,
  - the goods issue is posted per received item and the delivery back-filled,
  - a successful sync clears a stale hold, and held receipts can be re-queued.

ByD and the ORM managers are mocked, so no database is needed.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from byd_service.rest import RESTServices
from transfer_service import views
from transfer_service.admin import TransferReceiptNoteAdmin
from transfer_service.models import TransferReceiptNote
from vimp import tasks

SO_ID = "105143"


def sales_order(release_status="3", release_text="Released"):
	return {
		"ObjectID": "SOOBJ",
		"ID": SO_ID,
		"ReleaseStatusCode": release_status,
		"ReleaseStatusCodeText": release_text,
	}


def delivery_request(open_quantity="2.0", wrap_schedule_lines=False):
	schedule_lines = [{"OpenQuantity": {"Quantity": open_quantity}}]
	if wrap_schedule_lines:
		schedule_lines = {"results": schedule_lines}
	return {
		"ObjectID": "REQOBJ",
		"BaseBusinessTransactionDocumentID": "136999",
		"Item": [{
			"ObjectID": "REQITEM10",
			"BaseBusinessTransactionDocumentItemID": "10",
			"ProductID": "FP3000008",
			"ItemScheduleLine": schedule_lines,
		}],
	}


def build_receipt(received="2"):
	inbound_delivery = MagicMock(
		sales_order_reference=SO_ID, object_id=None, delivery_id=None,
		delivery_type_code=None, metadata={},
	)
	line = MagicMock(quantity_received=Decimal(received))
	line.inbound_delivery_line_item.metadata = {"ID": "10"}
	line.inbound_delivery_line_item.product_id = "FP3000008"
	receipt = MagicMock(receipt_number=1051431, metadata={}, inbound_delivery=inbound_delivery)
	receipt.line_items.select_related.return_value = [line]
	return receipt


def byd_client(order=None, request=None):
	"""A RESTServices stand-in that keeps the real exception classes."""
	client = MagicMock()
	client.DeliveryRequestNotFound = RESTServices.DeliveryRequestNotFound
	client.DeliveryRequestAmbiguous = RESTServices.DeliveryRequestAmbiguous
	client.get_sales_order_by_id.return_value = order or sales_order()
	if request is None:
		client.find_delivery_request_for_sales_order.side_effect = RESTServices.DeliveryRequestNotFound(
			f"No delivery request is linked to sales order {SO_ID}"
		)
	else:
		client.find_delivery_request_for_sales_order.return_value = request
	return client


@override_settings(BYD_AUTO_POST_GOODS_ISSUE=True, BYD_SHORT_RECEIPT_POLICY='hold')
@patch('vimp.tasks.time.sleep')
class CreateOutboundDeliveryForReceiptTests(SimpleTestCase):

	def run_with(self, client, receipt):
		with patch('vimp.tasks.byd_rest.RESTServices', return_value=client):
			tasks.create_outbound_delivery_for_receipt(receipt)

	def test_unreleased_sales_order_is_held_before_any_lookup(self, _sleep):
		client = byd_client(order=sales_order("1", "Not Released"))
		client.get_sales_order_outbound_deliveries.return_value = []

		with self.assertRaisesMessage(tasks.GoodsIssueHeld, "is Not Released in ByD"):
			self.run_with(client, build_receipt())

		client.find_delivery_request_for_sales_order.assert_not_called()
		client.post_goods_issue_for_request_item.assert_not_called()

	def test_missing_delivery_request_is_held_with_actionable_reason(self, _sleep):
		client = byd_client()
		client.get_sales_order_outbound_deliveries.return_value = []

		with self.assertRaises(tasks.GoodsIssueHeld) as held:
			self.run_with(client, build_receipt())

		self.assertIn(f"No delivery request is linked to sales order {SO_ID}", str(held.exception))
		self.assertIn("Release the customer demand", str(held.exception))
		client.post_goods_issue_for_request_item.assert_not_called()

	def test_goods_issue_posted_per_item_and_delivery_backfilled(self, _sleep):
		client = byd_client(request=delivery_request(wrap_schedule_lines=True))
		client.get_sales_order_outbound_deliveries.side_effect = [
			[],  # nothing delivered yet
			[{"ID": "92400", "UUID": "AB-CD", "ObjectID": "ABCD"}],  # visible after the goods issue
		]
		client.get_outbound_delivery_by_object_id.return_value = {"ID": "92400", "DeliveryTypeCode": "LF"}
		receipt = build_receipt(received="2")

		self.run_with(client, receipt)

		client.post_goods_issue_for_request_item.assert_called_once_with("REQITEM10", auto_release=True)
		self.assertEqual(receipt.inbound_delivery.object_id, "ABCD")
		self.assertEqual(receipt.inbound_delivery.delivery_id, "92400")
		self.assertEqual(receipt.metadata['byd_goods_issue_items'], ["REQITEM10"])

	def test_existing_delivery_is_linked_without_posting(self, _sleep):
		client = byd_client()
		client.get_sales_order_outbound_deliveries.return_value = [{"ID": "92400", "UUID": "AB-CD", "ObjectID": "ABCD"}]
		client.get_outbound_delivery_by_object_id.return_value = {"ID": "92400"}
		receipt = build_receipt()

		self.run_with(client, receipt)

		client.get_sales_order_by_id.assert_not_called()
		client.post_goods_issue_for_request_item.assert_not_called()
		self.assertEqual(receipt.inbound_delivery.object_id, "ABCD")


class SyncApprovedReceiptTests(SimpleTestCase):

	def approved_receipt(self, metadata):
		receipt = build_receipt()
		receipt.approval_status = 'approved'
		receipt.synced_to_sap = False
		receipt.metadata = metadata
		receipt.inbound_delivery.is_fully_received = True
		return receipt

	def test_hold_reason_recorded_and_receipt_left_unsynced(self):
		receipt = self.approved_receipt({})
		with patch.object(TransferReceiptNote, 'objects') as objects, \
				patch('vimp.tasks.create_outbound_delivery_for_receipt', side_effect=tasks.GoodsIssueHeld("customer demand not released")), \
				patch('vimp.tasks.post_goods_receipt_on_byd') as post_receipt:
			objects.select_related.return_value.get.return_value = receipt
			tasks.sync_approved_receipt_to_sap(1)

		self.assertEqual(receipt.metadata['byd_goods_issue_held']['reason'], "customer demand not released")
		self.assertFalse(receipt.synced_to_sap)
		post_receipt.assert_not_called()

	def test_successful_retry_clears_stale_hold(self):
		receipt = self.approved_receipt({'byd_goods_issue_held': {'reason': 'old', 'at': 'then'}})
		with patch.object(TransferReceiptNote, 'objects') as objects, \
				patch('vimp.tasks.create_outbound_delivery_for_receipt'), \
				patch('vimp.tasks.post_goods_receipt_on_byd'):
			objects.select_related.return_value.get.return_value = receipt
			tasks.sync_approved_receipt_to_sap(1)

		self.assertTrue(receipt.synced_to_sap)
		self.assertNotIn('byd_goods_issue_held', receipt.metadata)
		self.assertIn('sap_sync', receipt.metadata)


class SalesOrderSearchTests(SimpleTestCase):

	@patch('transfer_service.views.InboundDelivery')
	@patch('transfer_service.views.RESTServices')
	def test_unreleased_sales_order_is_refused_and_not_cached(self, rest_services, inbound_delivery):
		inbound_delivery.objects.filter.return_value.order_by.return_value.first.return_value = None
		rest_services.return_value.get_sales_order_by_id.return_value = sales_order("1", "Not Released")
		request = APIRequestFactory().get(f"/transfers/v1/deliveries/{SO_ID}/")
		force_authenticate(request, user=MagicMock(is_authenticated=True))

		response = views.get_inbound_delivery(request, pk=SO_ID)

		self.assertEqual(response.status_code, 404)
		self.assertIn("is Not Released in SAP ByD", str(response.data))
		inbound_delivery.create_from_sales_order_data.assert_not_called()


class RetryByDSyncActionTests(SimpleTestCase):

	@patch('transfer_service.admin.async_task')
	def test_only_approved_unsynced_receipts_are_requeued(self, async_task):
		model_admin = TransferReceiptNoteAdmin(TransferReceiptNote, AdminSite())
		model_admin.message_user = MagicMock()
		held = MagicMock(id=1, approval_status='approved', synced_to_sap=False)
		synced = MagicMock(id=2, approval_status='approved', synced_to_sap=True)
		pending = MagicMock(id=3, approval_status='pending', synced_to_sap=False)

		model_admin.retry_byd_sync(MagicMock(), [held, synced, pending])

		async_task.assert_called_once_with('vimp.tasks.sync_approved_receipt_to_sap', 1)
