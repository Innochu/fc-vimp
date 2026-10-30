from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from vimp import tasks


def _receipt(lines):
	"""lines: [(so_item_id, product_id, received)] -> stand-in TransferReceiptNote."""
	line_items = [
		SimpleNamespace(
			quantity_received=received,
			inbound_delivery_line_item=SimpleNamespace(
				product_id=product_id,
				metadata={"ID": so_item_id, "ItemScheduleLine": [{"TypeCode": "1", "unitCode": "EA"}]},
			),
		) for so_item_id, product_id, received in lines
	]
	manager = mock.MagicMock()
	manager.select_related.return_value = line_items
	return SimpleNamespace(
		receipt_number="1051621",
		metadata={},
		save=mock.Mock(),
		line_items=manager,
		inbound_delivery=SimpleNamespace(sales_order_reference="105162", save=mock.Mock(), metadata={}, delivery_type_code=""),
	)


def _request_item(so_item_id, open_quantity):
	return {
		"ObjectID": f"REQ-ITEM-{so_item_id}",
		"BaseBusinessTransactionDocumentItemID": so_item_id,
		"ItemScheduleLine": [{"OpenQuantity": {"Quantity": str(open_quantity)}}],
	}


@override_settings(BYD_AUTO_POST_GOODS_ISSUE=True)
class OutboundDeliveryForReceiptTests(SimpleTestCase):
	def _run(self, receipt, request_items):
		client = mock.Mock()
		client.DeliveryRequestNotFound = Exception
		client.DeliveryRequestAmbiguous = Exception
		client.get_sales_order_outbound_deliveries.return_value = []
		client.get_sales_order_by_id.return_value = {"ID": "105162"}
		client.find_delivery_request_for_sales_order.return_value = {"ID": "53", "Item": request_items}
		client.create_outbound_delivery_from_request.return_value = {"ObjectID": "DELOBJ", "ID": "11"}
		client.get_outbound_delivery_by_object_id.return_value = {"ID": "11", "DeliveryTypeCode": "STOD"}
		with mock.patch.object(tasks.byd_rest, "RESTServices", return_value=client):
			tasks.create_outbound_delivery_for_receipt(receipt)
		return client

	def test_short_receipt_delivers_received_quantity(self):
		receipt = _receipt([("10", "FP3000009", 1.5)])
		client = self._run(receipt, [_request_item("10", 2.0)])

		(_request, items), _ = client.create_outbound_delivery_from_request.call_args
		self.assertEqual(items, [{"so_item_id": "10", "product_id": "FP3000009", "quantity": 1.5, "unit_code": "EA"}])
		self.assertEqual(receipt.metadata["byd_short_receipt"][0]["received"], 1.5)
		client.release_outbound_delivery.assert_called_once_with("DELOBJ")
		self.assertNotIn("byd_delivery_pending_release", receipt.metadata)

	def test_multiple_lines_create_a_single_delivery(self):
		receipt = _receipt([("10", "FP1", 2.0), ("20", "FP2", 3.0), ("30", "FP3", 1.0)])
		client = self._run(receipt, [_request_item("10", 2.0), _request_item("20", 5.0), _request_item("30", 1.0)])

		client.create_outbound_delivery_from_request.assert_called_once()
		(_request, items), _ = client.create_outbound_delivery_from_request.call_args
		self.assertEqual([(i["so_item_id"], i["quantity"]) for i in items], [("10", 2.0), ("20", 3.0), ("30", 1.0)])
		client.post_goods_issue_for_request_item.assert_not_called()
		client.release_outbound_delivery.assert_called_once()

	def test_over_receipt_is_held(self):
		receipt = _receipt([("10", "FP1", 3.0)])
		with self.assertRaises(tasks.GoodsIssueHeld):
			self._run(receipt, [_request_item("10", 2.0)])

	def test_failed_release_resumes_without_creating_another_delivery(self):
		receipt = _receipt([("10", "FP1", 2.0)])
		receipt.metadata["byd_delivery_pending_release"] = "DELOBJ"
		client = self._run(receipt, [_request_item("10", 2.0)])

		client.create_outbound_delivery_from_request.assert_not_called()
		client.release_outbound_delivery.assert_called_once_with("DELOBJ")
