from django.urls import path
from . import views

app_name = 'gsr_service'

urlpatterns = [
	# Look up a PO and see what is still outstanding to confirm.
	path('purchaseorders/<int:po_id>', views.get_service_purchase_order),
	# GET: history (filterable, paginated). POST: confirm a service.
	path('receipts', views.receipts),
	path('receipts/<int:gsr_number>', views.get_receipt),
	path('receipts/<int:gsr_number>/attachments', views.add_attachments),
]
