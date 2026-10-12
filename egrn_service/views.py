# Import necessary modules and classes
import os, sys
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

from django.conf import settings
from django.forms import model_to_dict
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes
from django_auth_adfs.rest_framework import AdfsAccessTokenAuthentication
from overrides.authenticate import CombinedAuthentication
from overrides.rest_framework import CustomPagination
from byd_service.rest import RESTServices
from django.contrib.auth import get_user_model
from overrides.rest_framework import APIResponse
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q, Sum, Case, When, Value, CharField, F
from django.db.models.functions import Coalesce
from openpyxl import Workbook
from core_service.cache_utils import (
    cache_result, CacheManager, get_or_set_cache, 
    invalidate_user_cache, CachedPagination
)
from collections import defaultdict

from .models import (
	GoodsReceivedNote, GoodsReceivedLineItem, PurchaseOrder,
	PurchaseOrderLineItem, ProductConfiguration, Store,
	StockConsumptionRecord
)
from .serializers import GoodsReceivedNoteSerializer, GoodsReceivedLineItemSerializer, PurchaseOrderSerializer


logger = logging.getLogger(__name__)

# Initialize REST services
byd_rest_services = RESTServices()
# Get the user model
User = get_user_model()
# Pagination
paginator = CustomPagination()

GRN_EXPORT_HEADERS = [
	"PO Number",
	"GRN Number",
	"Vendor Name",
	"Vendor Code",
	"Date Created",
	"Store Name",
	"Store ByD Code",
	"Invoice Status",
	"Delivery Status",
	"Product Name",
	"Product Code",
	"Unit Price",
	"Quantity Received",
	"Net Value Received",
	"Gross Value Received",
	"Total Tax",
	"Outstanding Qty",
]

DELIVERY_STATUS_LOOKUP = dict(PurchaseOrder.delivery_status_code)


def delete_items(po):
	del po["Item"]
	return po

def filter_objects(keys_to_keep, objects):
	filtered_objects = []
	# Use dictionary comprehension to filter objects
	for obj in objects:
		filtered_obj = {key: obj[key] for key in keys_to_keep if key in obj}
		filtered_objects.append(filtered_obj)
	
	return filtered_objects

def get_formatted_vendor(id, id_type):
	data = byd_rest_services.get_vendor_by_id(id, id_type=id_type)
	vendor = {
		"InternalID": data["BusinessPartner"]["InternalID"],
		"CategoryCode": data["BusinessPartner"]["CategoryCode"],
		"CategoryCodeText": data["BusinessPartner"]["CategoryCodeText"],
		"BusinessPartnerFormattedName": data["BusinessPartner"]["BusinessPartnerFormattedName"],
	}
	
	return vendor


@api_view(['GET'])
@authentication_classes([AdfsAccessTokenAuthentication,])
def search_vendor(request, ):
	params = dict(request.GET)
	try:
		query_param = ('email', params['email'][0]) if params['email'] else ('phone', params['phone'][0])
		# Fetch purchase orders for the authenticated user
		vendor = get_formatted_vendor(query_param[1], query_param[1])
		# Data object to hold the return data
		data = {}
		
		if vendor:
			keys_to_keep = ["ObjectID", "UUID", "ID", "CreationDateTime", "LastChangeDateTime", "CurrencyCode",
							"CurrencyCodeText", "TotalGrossAmount", "TotalNetAmount", "TotalTaxAmount",
							"ConsistencyStatusCode",
							"LifeCycleStatusCode", "AcknowledgmentStatusCode", "AcknowledgmentStatusCodeText",
							"DeliveryStatusCode", "DeliveryStatusCodeText", "InvoicingStatusCode",
							"InvoicingStatusCodeText"]
			
			purchase_orders = byd_rest_services.get_vendor_purchase_orders(vendor["InternalID"])
			purchase_orders = filter_objects(keys_to_keep, list(map(delete_items, purchase_orders)))
			
			data["BusinessPartner"] = vendor
			data["PurchaseOrders"] = purchase_orders
			
			return APIResponse("Vendor found.", status.HTTP_200_OK, data=data)
		
		return APIResponse(f"No vendor results found for {query_param[1]} {query_param[1]}.", status.HTTP_404_NOT_FOUND)
	except Exception as e:
		logging.error(e)
		return APIResponse("Internal Error.", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_purchase_order(request, po_id):
	try:
		user_stores = Store.objects.filter(store_email=request.user.email)
		try:
			# Fetch purchase orders from the database
			orders = PurchaseOrder.objects.get(po_id=po_id)
		except ObjectDoesNotExist:
			# If the order does not exist in the database, fetch the order from ByD
			byd_orders = byd_rest_services.get_purchase_order_by_id(po_id)
			if byd_orders:
				# If the order exists in ByD, create a new PurchaseOrder object
				po = PurchaseOrder()
				orders = po.create_purchase_order(byd_orders)
			else:
				# If the order does not exist in ByD, return an error
				return APIResponse(f"Order with ID {po_id} not found.", status.HTTP_404_NOT_FOUND)
		# Serialize the PurchaseOrder object
		serializer = PurchaseOrderSerializer(orders).data
		serializer["Item"] = list(
			filter(lambda x: x.get('delivery_store').get('id') in [s.id for s in user_stores], serializer["Item"])
		)
		if len(serializer["Item"]) > 0:
			serializer["stores"] = filter(
				lambda x: x.id in [s for s in map(lambda x: x["delivery_store"], serializer["Item"])],
				user_stores
			)
			return APIResponse("Purchase Orders Retrieved", status.HTTP_200_OK, data=serializer)
		else:
			return APIResponse(f"No orders found in {po_id} for your stores: {', '.join([s.store_name for s in user_stores])}.", status.HTTP_404_NOT_FOUND)
	except Exception as e:
		# Handle any other errors
		logging.error(f"An error occurred creating a Purchase Order: {e}")
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)

	
@api_view(['POST'])
@authentication_classes([AdfsAccessTokenAuthentication,])
def create_grn(request, ):
	identifier = "PONumber"  # should be PO_ID
	# keys we NEED to create a GRN
	required_keys = [identifier, "recievedGoods"]
	# the post request
	request_data = request.data
	# Check that all the required keys are present in the request
	required_keys_present = [
		any(
			map(lambda x: r in x, list(request_data.keys()))
		) for r in required_keys
	]
	# If required keys are not present, return an error
	if not all(required_keys_present):
		return APIResponse(f"Missing required key(s) [{', '.join(required_keys)}]", status.HTTP_400_BAD_REQUEST)
	# Make the PO_ID key consistent as the identifier
	request_data["po_id"] = request_data[identifier]
	try:
		# Filter for only the PO Line items that the user has permission to receive
		permitted_to_receive_items = (PurchaseOrderLineItem.objects.filter(object_id__in=map(lambda x: x['itemObjectID'], request_data["recievedGoods"]))
							.filter(delivery_store__store_email=request.user.email))
		# If there are no items that the user has permission to receive, return an error
		if not permitted_to_receive_items:
			return APIResponse("User does not have permission to receive these items.", status.HTTP_403_FORBIDDEN)
		# Filter the request data to only include the items that the user has permission to receive
		request_data["recievedGoods"] = list(filter(lambda x: x['itemObjectID'] in [i.object_id for i in permitted_to_receive_items], request_data["recievedGoods"]))
		
		# Create the GRN
		created_grn = GoodsReceivedNote().save(grn_data=request_data)
		# Serialize the GoodsReceivedNote instance along with its related GoodsReceivedLineItem instances
		goods_received_note = GoodsReceivedNoteSerializer(created_grn).data
		return APIResponse("GRN Created.", status.HTTP_201_CREATED, data=goods_received_note)
	# Return an error if there is an exception
	except Exception as e:
		return APIResponse(str(e), status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_all_grns(request, ):
	try:
		# Cache user stores lookup
		user_stores_key = CacheManager.get_user_cache_key(
			request.user, "stores", request.user.email
		)
		user_stores = get_or_set_cache(
			user_stores_key,
			lambda: list(Store.objects.filter(store_email=request.user.email)),
			CacheManager.TIMEOUT_LONG
		)
		
		if not user_stores:
			return APIResponse(f"No stores found for user: {request.user.email}", status.HTTP_404_NOT_FOUND)
		
		# Generate cache key for this user's GRN query
		page = request.query_params.get('page', '1')
		page_size = request.query_params.get('size', '15')
		cache_key_suffix = f"all_grns_user_{request.user.id}_page_{page}_size_{page_size}"
		
		# Get all GRNs with optimized queries to reduce database hits
		grns = GoodsReceivedNote.objects.select_related(
			'purchase_order',
			'purchase_order__vendor'
		).prefetch_related(
			'line_items__purchase_order_line_item__delivery_store'
		).filter(
			line_items__purchase_order_line_item__delivery_store__in=user_stores
		).distinct()
		
		if grns.exists():
			# Cache the total count for pagination
			total_count = CachedPagination.cache_page_count(grns, cache_key_suffix)
			
			# Paginate the results - now only fetches the requested page from database
			paginated = paginator.paginate_queryset(grns, request, order_by='-id')
			# Serialize the GoodsReceivedNote instance along with its related GoodsReceivedLineItem instances
			grn_serializer = GoodsReceivedNoteSerializer(paginated, many=True, context={'request':request})
			# Return the paginated response with the serialized GoodsReceivedNote instances
			paginated_data = paginator.get_paginated_response(grn_serializer.data).data
			return APIResponse("GRNs Retrieved", status.HTTP_200_OK, data=paginated_data)
		return APIResponse(f"No GRNs found for your stores: {', '.join([s.store_name for s in user_stores])}", status.HTTP_404_NOT_FOUND)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def filter_grns(request, ):
	'''
		Get GRNs based on the given filters, if any.
		The filters are:
			- 'po_id': Filter GRNs by purchase order ID
			- 'date_created': Filter GRNs by creation date
			- 'delivery_stores': Filter GRNs by delivery store ID
			- 'delivery_status_code': Filter GRNs by delivery status code
			- 'invoice_status_code': Filter GRNs by invoice status code
			- 'start_date': Filter GRNs by start date
			- 'end_date': Filter GRNs by end date
			- 'vendor_internal_id': Filter GRNs by vendor internal ID (from ByD)
	'''
	# Build all filters at database level for efficient querying
	try:
		grns = _build_filtered_grns_queryset(request)
		
		if grns.exists():
			paginated = paginator.paginate_queryset(grns, request)
			serialized_data = GoodsReceivedNoteSerializer(paginated, many=True, context={'request': request}).data
			return paginator.get_paginated_response(serialized_data)
		return APIResponse("No GRNs found for the specified criteria.", status=status.HTTP_404_NOT_FOUND)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def download_grns(request):
	try:
		grns = _build_filtered_grns_queryset(request)
		if not grns.exists():
			return APIResponse("No GRNs found for the specified criteria.", status=status.HTTP_404_NOT_FOUND)
		
		grn_ids = list(grns.values_list('id', flat=True))
		if not grn_ids:
			return APIResponse("No GRNs found for the specified criteria.", status=status.HTTP_404_NOT_FOUND)
		
		line_items_map, delivered_quantity_map = _collect_grn_line_items(grn_ids)
		file_path, row_count = _write_grn_export_file(
			request=request,
			queryset=grns,
			line_items_map=line_items_map,
			delivered_quantity_map=delivered_quantity_map,
		)
		download_url = _build_media_download_url(request, file_path)
		
		return APIResponse(
			"Download ready.",
			status=status.HTTP_200_OK,
			data={
				"download_url": download_url,
				"row_count": row_count,
			}
		)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _build_filtered_grns_queryset(request):
	django_filters = {}
	store_lookup_q = None
	q_filters = None
	
	for key in request.query_params:
		value = request.query_params.get(key)
		if value in [None, '']:
			continue
			
		elif key == 'date_created':
			django_filters['created'] = value
		elif key == 'start_date':
			django_filters['created__gte'] = value
		elif key == 'end_date':
			django_filters['created__lte'] = value
		elif key == 'po_id':
			django_filters['purchase_order__po_id'] = value
		elif key == 'vendor_internal_id':
			django_filters['purchase_order__vendor__byd_internal_id'] = value
		elif key == 'delivery_stores':
			store_identifiers = [identifier.strip() for identifier in value.split(',') if identifier.strip()]
			if store_identifiers:
				store_lookup_q = Q()
				for identifier in store_identifiers:
					store_lookup_q |= Q(line_items__purchase_order_line_item__delivery_store__store_name__icontains=identifier)
					store_lookup_q |= Q(line_items__purchase_order_line_item__delivery_store__byd_cost_center_code__iexact=identifier)
	
	queryset = GoodsReceivedNote.objects.select_related(
		'purchase_order',
		'purchase_order__vendor',
		'purchase_order__vendor__user',
	).prefetch_related(
		'line_items__purchase_order_line_item__delivery_store'
	).filter(**django_filters).annotate(
		po_total_qty=Coalesce(Sum('purchase_order__line_items__quantity'), Decimal('0.0')),
		po_delivered_qty=Coalesce(Sum('purchase_order__line_items__grn_line_item__quantity_received'), Decimal('0.0')),
		invoice_quantity=Coalesce(Sum('line_items__invoice_items__quantity'), Decimal('0.0')),
		invoice_total_qty=Coalesce(Sum('line_items__quantity_received'), Decimal('0.0')),
	).annotate(
		delivery_status_code_db=Case(
			When(Q(po_delivered_qty__gte=F('po_total_qty')) & Q(po_total_qty__gt=0), then=Value('3')),
			When(po_delivered_qty__gt=0, then=Value('2')),
			default=Value('1'),
			output_field=CharField(),
		),
		delivery_status_text_db=Case(
			When(delivery_status_code_db='3', then=Value('Completely Delivered')),
			When(delivery_status_code_db='2', then=Value('Partially Delivered')),
			default=Value('Not Delivered'),
			output_field=CharField(),
		),
		invoice_status_code_db=Case(
			When(Q(invoice_quantity__gte=F('invoice_total_qty')) & Q(invoice_total_qty__gt=0), then=Value('3')),
			When(invoice_quantity__gt=0, then=Value('2')),
			default=Value('1'),
			output_field=CharField(),
		),
		invoice_status_text_db=Case(
			When(invoice_status_code_db='3', then=Value('Finished')),
			When(invoice_status_code_db='2', then=Value('In Process')),
			default=Value('Not Started'),
			output_field=CharField(),
		),
	)

	delivery_status_code = request.query_params.get('delivery_status_code')
	if delivery_status_code:
		queryset = queryset.filter(
			Q(delivery_status_code_db=delivery_status_code) |
			Q(delivery_status_text_db__icontains=delivery_status_code)
		)

	invoice_status_code = request.query_params.get('invoice_status_code')
	if invoice_status_code:
		queryset = queryset.filter(
			Q(invoice_status_code_db=invoice_status_code) |
			Q(invoice_status_text_db__icontains=invoice_status_code)
		)

	if q_filters:
		queryset = queryset.filter(q_filters)
	if store_lookup_q:
		queryset = queryset.filter(store_lookup_q)
	
	order_by = request.query_params.get('order_by', '-id')
	if order_by:
		queryset = queryset.order_by(order_by)
	
	return queryset.distinct()


def _collect_grn_line_items(grn_ids: list):
	line_items_map = defaultdict(list)
	delivered_quantity_map = defaultdict(lambda: Decimal('0'))

	if not grn_ids:
		return line_items_map, delivered_quantity_map

	line_items = GoodsReceivedLineItem.objects.filter(
		grn_id__in=grn_ids
	).select_related(
		'purchase_order_line_item__delivery_store'
	)

	for line_item in line_items.iterator(chunk_size=1000):
		grn_id = line_item.grn_id
		po_line_item = line_item.purchase_order_line_item
		delivered_quantity_map[po_line_item.id] += line_item.quantity_received or Decimal('0')

		delivery_store = getattr(po_line_item, 'delivery_store', None)
		line_items_map[grn_id].append({
			'store_code': getattr(delivery_store, 'byd_cost_center_code', '') if delivery_store else '',
			'store_name': getattr(delivery_store, 'store_name', '') if delivery_store else '',
			'product_name': getattr(po_line_item, 'product_name', '') or '',
			'product_code': getattr(po_line_item, 'product_id', '') or '',
			'unit_price': po_line_item.unit_price or Decimal('0'),
			'quantity': line_item.quantity_received or Decimal('0'),
			'net_value': line_item.net_value_received or Decimal('0'),
			'gross_value': line_item.gross_value_received or Decimal('0'),
			'po_line_item_id': po_line_item.id,
			'total_quantity': po_line_item.quantity or Decimal('0'),
		})

	return line_items_map, delivered_quantity_map


def _write_grn_workbook(queryset, line_items_map, delivered_quantity_map, target):
	'''
		Write the GRN export workbook to `target` (a file path or a binary file-like object).
		Returns the number of data rows written.
	'''
	workbook = Workbook(write_only=True)
	worksheet = workbook.create_sheet(title="GRNs")
	worksheet.append(GRN_EXPORT_HEADERS)

	row_count = 0
	for grn in queryset.iterator(chunk_size=500):
		grn_rows = line_items_map.get(grn.id, [])
		for line_info in grn_rows:
			worksheet.append(
				_build_grn_export_row(
					grn,
					line_info,
					delivered_quantity_map,
				)
			)
			row_count += 1

	workbook.save(target)
	workbook.close()
	return row_count


def _write_grn_export_file(request, queryset, line_items_map, delivered_quantity_map):
	download_dir = _ensure_grn_download_dir()
	user_identifier = getattr(request.user, 'id', None) or 'anonymous'
	filename = f"grns_{user_identifier}_{timezone.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}.xlsx"
	file_path = os.path.join(download_dir, filename)

	row_count = _write_grn_workbook(queryset, line_items_map, delivered_quantity_map, file_path)
	return file_path, row_count


def _build_grn_export_row(grn, line_info, delivered_quantity_map):
	po = getattr(grn, 'purchase_order', None)
	vendor_profile = getattr(po, 'vendor', None) if po else None
	vendor_user = getattr(vendor_profile, 'user', None) if vendor_profile else None
	vendor_name = _format_vendor_name(vendor_user) if vendor_user else ''

	delivery_status = _get_delivery_status_text(
		line_info.get('total_quantity'),
		delivered_quantity_map.get(line_info.get('po_line_item_id'), Decimal('0'))
	)

	vendor_code = getattr(vendor_profile, 'byd_internal_id', '') if vendor_profile else ''
	total_quantity = _safe_decimal(line_info.get('total_quantity'))
	delivered_quantity = _safe_decimal(delivered_quantity_map.get(line_info.get('po_line_item_id'), Decimal('0')))
	total_tax = _safe_decimal(line_info.get('gross_value')) - _safe_decimal(line_info.get('net_value'))
	outstanding = max(total_quantity - delivered_quantity, Decimal('0'))

	return [
		getattr(po, 'po_id', ''),
		grn.grn_number,
		vendor_name,
		vendor_code,
		_format_datetime(grn.created),
		line_info.get('store_name', ''),
		line_info.get('store_code', ''),
		_format_invoice_status(grn),
		delivery_status,
		line_info.get('product_name', ''),
		line_info.get('product_code', ''),
		float(_safe_decimal(line_info.get('unit_price'))),
		float(_safe_decimal(line_info.get('quantity'))),
		float(_safe_decimal(line_info.get('net_value'))),
		float(_safe_decimal(line_info.get('gross_value'))),
		float(total_tax),
		float(outstanding),
	]


def _format_vendor_name(user):
	if not user:
		return ''
	full_name = (user.get_full_name() or '').strip()
	if full_name:
		return full_name
	if user.username:
		return user.username
	return user.email or ''


def _format_invoice_status(grn):
	code = getattr(grn, 'invoice_status_code', '')
	text = getattr(grn, 'invoice_status_text', '')
	if code or text:
		code_str = f"[{code}]" if code else ""
		return f"{code_str} {text}".strip()
	return ''


def _format_datetime(value):
	if not value:
		return ''
	if isinstance(value, datetime):
		if timezone.is_aware(value):
			value = timezone.localtime(value)
		return value.strftime('%Y-%m-%d %H:%M:%S')
	return value.strftime('%Y-%m-%d')


def _get_delivery_status_text(total_quantity, delivered_quantity):
	total = _safe_decimal(total_quantity)
	delivered = _safe_decimal(delivered_quantity)
	if total == 0:
		return ''
	if delivered == 0:
		status_code = '1'
	elif delivered < total:
		status_code = '2'
	else:
		status_code = '3'
	return DELIVERY_STATUS_LOOKUP.get(status_code, '')


def _decimal_to_string(value):
	if value is None:
		return "0"
	if isinstance(value, Decimal):
		value = value.normalize()
		s = format(value, 'f')
		s = s.rstrip('0').rstrip('.') if '.' in s else s
		return s or "0"
	return str(value)


def _safe_decimal(value):
	if isinstance(value, Decimal):
		return value
	if value is None:
		return Decimal('0')
	try:
		return Decimal(value)
	except (TypeError, InvalidOperation):
		try:
			return Decimal(str(value))
		except (TypeError, InvalidOperation):
			return Decimal('0')


def _ensure_grn_download_dir():
	download_dir = os.path.join(settings.MEDIA_ROOT, 'downloads', 'grns')
	os.makedirs(download_dir, exist_ok=True)
	return download_dir


def _build_media_download_url(request, file_path: str) -> str:
	relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT).replace(os.sep, '/')
	media_url = settings.MEDIA_URL or '/media/'
	if not media_url.endswith('/'):
		media_url = f"{media_url}/"
	if not media_url.startswith('/'):
		media_url = f"/{media_url}"
	return request.build_absolute_uri(f"{media_url}{relative_path}")
	

@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_vendors_grns(request, ):
	'''
		Get all GRNs for the authenticated user
	'''
	try:
		po_id = request.query_params.get('po_id')
		grns = GoodsReceivedNote.objects.select_related(
			'purchase_order',
			'purchase_order__vendor'
		).prefetch_related(
			'line_items__purchase_order_line_item__delivery_store'
		).filter(purchase_order__vendor=request.user.vendor_profile)
		# If the request params contain po_id, filter by po_id
		grns = grns.filter(purchase_order__po_id=po_id) if po_id else grns
		if grns.exists():
			# Paginate the results
			paginated = paginator.paginate_queryset(grns, request, order_by='-id')
			# Serialize the GoodsReceivedNote instance along with its related GoodsReceivedLineItem instances
			grn_serializer = GoodsReceivedNoteSerializer(paginated, many=True, context={'request':request})
			# Return the paginated response with the serialized GoodsReceivedNote instances
			paginated_data = paginator.get_paginated_response(grn_serializer.data).data
			return APIResponse("GRNs Retrieved", status.HTTP_200_OK, data=paginated_data)
		return APIResponse(f"No GRN found.", status=status.HTTP_404_NOT_FOUND)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def download_vendor_grns(request):
	'''
		Download the authenticated vendor's GRNs as an Excel file.
		Accepts the same filters as download-grns (po_id, start_date, end_date, ...),
		but is always limited to the requesting vendor's own GRNs. The file is streamed
		in the response rather than written to the public media folder.
	'''
	try:
		vendor_profile = getattr(request.user, 'vendor_profile', None)
		if vendor_profile is None:
			return APIResponse("Only vendors can download GRNs.", status=status.HTTP_403_FORBIDDEN)

		grns = _build_filtered_grns_queryset(request).filter(purchase_order__vendor=vendor_profile)
		if not grns.exists():
			return APIResponse("No GRNs found for the specified criteria.", status=status.HTTP_404_NOT_FOUND)

		grn_ids = list(grns.values_list('id', flat=True))
		line_items_map, delivered_quantity_map = _collect_grn_line_items(grn_ids)

		buffer = BytesIO()
		_write_grn_workbook(grns, line_items_map, delivered_quantity_map, buffer)
		buffer.seek(0)

		filename = f"grns_{vendor_profile.byd_internal_id}_{timezone.now().strftime('%Y%m%d%H%M%S')}.xlsx"
		response = HttpResponse(
			buffer.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)
		response['Content-Disposition'] = f'attachment; filename="{filename}"'
		return response
	except Exception as e:
		logger.exception(f"Error generating vendor GRN download: {e}")
		return APIResponse("Internal Error while generating the GRN download.", status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication])
def get_grn(request, grn_number):
	try:
		grn = GoodsReceivedNote.objects.get(grn_number=grn_number)
		if grn:
			# Serialize the GoodsReceivedNote instance along with its related GoodsReceivedLineItem instances
			grn_serializer = GoodsReceivedNoteSerializer(grn)
			goods_received_note = grn_serializer.data
			return APIResponse("GRN Retrieved", status.HTTP_200_OK, data=goods_received_note)
		else:
			return APIResponse("GRN Not Found", status=status.HTTP_404_NOT_FOUND)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@authentication_classes([CombinedAuthentication,])
def weighted_average(request):
	'''
		Get the weighted average cost for all products or for a specific product, with a history of purchases.
		Results are cached for 30 minutes to improve performance.
	'''
	def calculate_wac(events, product_name, product_id, cumulative_quantity, cumulative_cost):
		# Dictionary to store results grouped by product_id
		product_data = {
			"product_id": product_id,
			"product_name": product_name,
			"starting_quantity": cumulative_quantity,
			"starting_cost": cumulative_cost / cumulative_quantity if cumulative_quantity > 0 else 0,
			"cumulative_quantity": cumulative_quantity,
			"cumulative_cost": cumulative_cost,
			"wac": round(cumulative_cost / cumulative_quantity, 2) if cumulative_quantity > 0 else 0,
			"history": [],
		}

		def safe_wac(quantity, cost):
			return round(cost / quantity, 2) if quantity > 0 else 0

		for event in events:
			if event["type"] == "receive":
				line_item = event["line_item"]
				purchase_quantity = line_item.quantity_received
				purchase_cost = purchase_quantity * line_item.purchase_order_line_item.unit_price
				
				product_data["cumulative_quantity"] += purchase_quantity
				product_data["cumulative_cost"] += purchase_cost
				product_data["wac"] = safe_wac(
					product_data["cumulative_quantity"],
					product_data["cumulative_cost"],
				)

				product_data["history"].append({
					"event": "receipt",
					"date": line_item.date_received,
					"store": line_item.purchase_order_line_item.delivery_store.store_name,
					"purchase_quantity": purchase_quantity,
					"purchase_price_per_unit": line_item.purchase_order_line_item.unit_price,
					"purchase_cost": purchase_cost,
					"cumulative_quantity": product_data["cumulative_quantity"],
					"cumulative_cost": product_data["cumulative_cost"],
					"wac": product_data["wac"],
					"grn": GoodsReceivedLineItemSerializer(line_item).data,
				})
			else:
				record = event["record"]
				consumed_quantity = record.quantity
				consumed_cost = record.total_cost

				product_data["cumulative_quantity"] = max(product_data["cumulative_quantity"] - consumed_quantity, 0)
				product_data["cumulative_cost"] = max(product_data["cumulative_cost"] - consumed_cost, 0)
				product_data["wac"] = safe_wac(
					product_data["cumulative_quantity"],
					product_data["cumulative_cost"],
				)

				product_data["history"].append({
					"event": "consumption",
					"date": record.date_consumed,
					"store": record.cost_center,
					"consumed_quantity": consumed_quantity,
					"consumption_unit_cost": record.unit_cost,
					"consumption_cost": consumed_cost,
					"cumulative_quantity": product_data["cumulative_quantity"],
					"cumulative_cost": product_data["cumulative_cost"],
					"wac": product_data["wac"],
					"metadata": record.metadata,
				})

		return product_data

	products_wac = []
	
	try:
		if request.query_params.get('product_id'):
			products = [x.strip() for x in request.query_params.get('product_id').split(',') if x.strip()]
		else:
			# Optimize distinct product query
			products = GoodsReceivedLineItem.objects.select_related(
				'purchase_order_line_item'
			).values_list('purchase_order_line_item__product_id', flat=True).distinct()
		
		# Optimize product config queries by fetching all at once
		product_configs = {
			pc.product_id: pc for pc in 
			ProductConfiguration.objects.filter(product_id__in=products)
		}

		# Track stock consumption records per product
		consumption_records_by_product = defaultdict(list)
		consumption_queryset = StockConsumptionRecord.objects.filter(
			product_id__in=products
		).order_by('product_id', 'date_consumed')
		for record in consumption_queryset:
			consumption_records_by_product[record.product_id].append(record)
		
		# Apply date filter at the queryset level if provided
		date_filter = {}
		if request.query_params.get('date'):
			try:
				date_filter['date_received__lte'] = datetime.strptime(
					request.query_params.get('date'),
					'%Y-%m-%d'
				)
			except ValueError:
				return APIResponse("Invalid date format. Use YYYY-MM-DD.", status.HTTP_400_BAD_REQUEST)
		
		# Fetch all relevant line items with optimized query
		base_queryset = GoodsReceivedLineItem.objects.select_related(
			'purchase_order_line_item__delivery_store'
		).filter(
			purchase_order_line_item__product_id__in=products,
			**date_filter
		).order_by('purchase_order_line_item__product_id', 'date_received')
		
		# Group line items by product to avoid multiple queries
		from itertools import groupby
		line_items_by_product = groupby(
			base_queryset, 
			key=lambda x: x.purchase_order_line_item.product_id
		)
		
		for product_id, line_items_group in line_items_by_product:
			line_items_list = list(line_items_group)
			if not line_items_list and not consumption_records_by_product.get(product_id):
				continue

			# Get product config data efficiently
			product_config = product_configs.get(product_id)
			cumulative_quantity, cumulative_cost = 0, 0
			if product_config and hasattr(product_config, 'metadata') and product_config.metadata:
				cumulative_quantity = product_config.metadata.get('inital_quantity', 0)
				cumulative_cost = product_config.metadata.get('initial_cost', 0) * cumulative_quantity

			events = []
			for line_item in line_items_list:
				events.append({
					"type": "receive",
					"date": line_item.date_received,
					"line_item": line_item,
					"product_id": product_id,
				})
			for record in consumption_records_by_product.get(product_id, []):
				events.append({
					"type": "consumption",
					"date": record.date_consumed,
					"record": record,
					"product_id": product_id,
				})

			events.sort(key=lambda event: event["date"])

			product_name = (
				line_items_list[0].purchase_order_line_item.product_name
				if line_items_list else
				product_config.product_name if product_config else
				product_id
			)

			products_wac.append(
				calculate_wac(events, product_name, product_id, cumulative_quantity, cumulative_cost)
			)
			
		# Paginate the results
		paginated = paginator.paginate_queryset(products_wac, request)
		paginated_data = paginator.get_paginated_response(paginated).data
		return APIResponse("Weighted Averages Calculated", status.HTTP_200_OK, data=paginated_data)
	except Exception as e:
		return APIResponse(f"Internal Error: {e}", status.HTTP_500_INTERNAL_SERVER_ERROR)