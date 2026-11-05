import logging
import os
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django_q.tasks import async_task
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes

from byd_service.rest import RESTServices
from egrn_service.models import PurchaseOrder, PurchaseOrderLineItem
from overrides.authenticate import CombinedAuthentication
from overrides.rest_framework import APIResponse, CustomPagination

from .access import get_user_stores
from .models import GoodsServiceReceipt, GSRLineItem, GSRAttachment
from .serializers import GoodsServiceReceiptSerializer

logger = logging.getLogger(__name__)
paginator = CustomPagination()
byd_rest_services = RESTServices()

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.doc', '.docx', '.xls', '.xlsx'}


def _is_service_line(line_item) -> bool:
	'''
		A line is treated as a maintenance service if its ByD product ID is listed in
		settings.GSR_SERVICE_PRODUCT_IDS. An empty list means no restriction.
	'''
	service_ids = getattr(settings, 'GSR_SERVICE_PRODUCT_IDS', [])
	return not service_ids or line_item.product_id in service_ids


def _can_confirm(user) -> bool:
	if getattr(settings, 'GSR_REQUIRE_CONFIRM_PERMISSION', False):
		return user.has_perm('gsr_service.confirm_service')
	return True


def _get_purchase_order(po_id):
	'''Fetch the PO from the database, falling back to ByD. Returns None if ByD has no such PO.'''
	try:
		return PurchaseOrder.objects.get(po_id=po_id)
	except ObjectDoesNotExist:
		byd_order = byd_rest_services.get_purchase_order_by_id(po_id)
		if not byd_order:
			return None
		return PurchaseOrder().create_purchase_order(byd_order)


def _user_receipts(user):
	return (GoodsServiceReceipt.objects
			.filter(store__in=get_user_stores(user))
			.select_related('purchase_order', 'purchase_order__vendor', 'store', 'created_by')
			.prefetch_related('line_items__purchase_order_line_item', 'attachments'))


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_service_purchase_order(request, po_id):
	'''
		Look up a PO for the user's stores and show, per service line, how much is
		still outstanding to be confirmed.
	'''
	try:
		user_stores = list(get_user_stores(request.user))
		order = _get_purchase_order(po_id)
		if order is None:
			return APIResponse(f"Order with ID {po_id} not found.", status.HTTP_404_NOT_FOUND)

		lines = []
		for item in order.line_items.select_related('delivery_store'):
			if item.delivery_store not in user_stores or not _is_service_line(item):
				continue
			lines.append({
				"itemObjectID": item.object_id,
				"product_id": item.product_id,
				"product_name": item.product_name,
				"unit_of_measurement": item.unit_of_measurement,
				"unit_price": item.unit_price,
				"quantity_ordered": item.quantity,
				"quantity_outstanding": GSRLineItem.outstanding_quantity(item),
				"store_name": item.delivery_store.store_name,
				"store_code": item.delivery_store.byd_cost_center_code,
			})
		if not lines:
			return APIResponse(f"No service lines found in {po_id} for your stores.", status.HTTP_404_NOT_FOUND)
		return APIResponse("Purchase Order Retrieved", status.HTTP_200_OK, data={
			"po_id": order.po_id,
			"vendor": order.vendor.byd_internal_id,
			"lines": lines,
		})
	except Exception as e:
		logger.error(f"Error retrieving PO {po_id} for GSR: {e}")
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET', 'POST'])
@authentication_classes([CombinedAuthentication])
def receipts(request):
	if request.method == 'POST':
		return _create_receipt(request)
	return _list_receipts(request)


def _create_receipt(request):
	'''
		POST body: {"po_id": 123, "notes": "...", "lines": [{"itemObjectID": "...", "quantity": 1}]}
	'''
	if not _can_confirm(request.user):
		return APIResponse("You do not have permission to confirm services.", status.HTTP_403_FORBIDDEN)

	data = request.data
	po_id, lines_data = data.get("po_id"), data.get("lines")
	if not po_id or not isinstance(lines_data, list) or not lines_data:
		return APIResponse("'po_id' and a non-empty 'lines' list are required.", status.HTTP_400_BAD_REQUEST)

	try:
		order = _get_purchase_order(po_id)
		if order is None:
			return APIResponse(f"Order with ID {po_id} not found.", status.HTTP_404_NOT_FOUND)

		user_stores = list(get_user_stores(request.user))
		lines, stores = [], set()
		for entry in lines_data:
			object_id = entry.get("itemObjectID")
			try:
				quantity = Decimal(str(entry.get("quantity")))
			except (InvalidOperation, TypeError):
				return APIResponse(f"Invalid quantity for line {object_id}.", status.HTTP_400_BAD_REQUEST)
			try:
				item = order.line_items.select_related('delivery_store').get(object_id=object_id)
			except ObjectDoesNotExist:
				return APIResponse(f"Line {object_id} is not on PO {po_id}.", status.HTTP_400_BAD_REQUEST)
			if item.delivery_store not in user_stores:
				return APIResponse("User does not have permission to confirm these items.", status.HTTP_403_FORBIDDEN)
			if not _is_service_line(item):
				return APIResponse(f"Line {object_id} is not a service item.", status.HTTP_400_BAD_REQUEST)
			if any(existing is item or existing.id == item.id for existing, _ in lines):
				return APIResponse(f"Line {object_id} appears more than once.", status.HTTP_400_BAD_REQUEST)
			lines.append((item, quantity))
			stores.add(item.delivery_store_id)

		if len(stores) > 1:
			return APIResponse("One receipt can only cover one store.", status.HTTP_400_BAD_REQUEST)

		receipt = GoodsServiceReceipt.create_receipt(
			order, lines[0][0].delivery_store, request.user, lines, notes=data.get("notes", "")
		)
		async_task('gsr_service.tasks.post_gsr_to_byd', receipt.id, q_options={
			'task_name': f'Post-GSR-{receipt.gsr_number}-To-ByD',
		})
		return APIResponse("GSR Created.", status.HTTP_201_CREATED,
						   data=GoodsServiceReceiptSerializer(receipt, context={'request': request}).data)
	except ValidationError as e:
		return APIResponse("; ".join(e.messages), status.HTTP_400_BAD_REQUEST)
	except Exception as e:
		logger.error(f"Error creating GSR: {e}")
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


def _list_receipts(request):
	'''
		Filters: status, po_id, store (name or ByD code), start_date, end_date (YYYY-MM-DD).
	'''
	try:
		queryset = _user_receipts(request.user)
		params = request.query_params
		if params.get('status'):
			queryset = queryset.filter(status=params['status'])
		if params.get('po_id'):
			queryset = queryset.filter(purchase_order__po_id=params['po_id'])
		if params.get('store'):
			store = params['store']
			queryset = queryset.filter(store__store_name__icontains=store) | \
				queryset.filter(store__byd_cost_center_code__iexact=store)
		if params.get('start_date'):
			queryset = queryset.filter(created__date__gte=params['start_date'])
		if params.get('end_date'):
			queryset = queryset.filter(created__date__lte=params['end_date'])

		if not queryset.exists():
			return APIResponse("No GSRs found for the specified criteria.", status.HTTP_404_NOT_FOUND)
		page = paginator.paginate_queryset(queryset.distinct(), request, order_by='-id')
		data = GoodsServiceReceiptSerializer(page, many=True, context={'request': request}).data
		return paginator.get_paginated_response(data)
	except Exception as e:
		logger.error(f"Error listing GSRs: {e}")
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_receipt(request, gsr_number):
	try:
		receipt = _user_receipts(request.user).get(gsr_number=gsr_number)
	except ObjectDoesNotExist:
		return APIResponse(f"GSR {gsr_number} not found.", status.HTTP_404_NOT_FOUND)
	return APIResponse("GSR Retrieved", status.HTTP_200_OK,
					   data=GoodsServiceReceiptSerializer(receipt, context={'request': request}).data)


@api_view(['POST'])
@authentication_classes([CombinedAuthentication])
def add_attachments(request, gsr_number):
	'''Attach completion evidence. Multipart form with one or more "files" fields.'''
	try:
		receipt = _user_receipts(request.user).get(gsr_number=gsr_number)
	except ObjectDoesNotExist:
		return APIResponse(f"GSR {gsr_number} not found.", status.HTTP_404_NOT_FOUND)

	files = request.FILES.getlist('files')
	if not files:
		return APIResponse("No files uploaded (use the 'files' field).", status.HTTP_400_BAD_REQUEST)
	for f in files:
		ext = os.path.splitext(f.name)[1].lower()
		if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
			return APIResponse(f"File type '{ext}' is not allowed.", status.HTTP_400_BAD_REQUEST)
		if f.size > MAX_ATTACHMENT_BYTES:
			return APIResponse(f"'{f.name}' is larger than 10 MB.", status.HTTP_400_BAD_REQUEST)
	for f in files:
		GSRAttachment.objects.create(gsr=receipt, file=f, original_name=f.name, uploaded_by=request.user)
	receipt.refresh_from_db()
	return APIResponse("Attachments added.", status.HTTP_201_CREATED,
					   data=GoodsServiceReceiptSerializer(receipt, context={'request': request}).data)
