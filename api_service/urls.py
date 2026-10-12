from django.urls import path, include
from .views import *
from invoice_service.views import *
from approval_service.views import *
from core_service.views import login_user, verify_otp, PasswordResetRequestView, PasswordResetView, PasswordChangeView
from egrn_service.views import get_vendors_grns, download_vendor_grns

urlpatterns = [
	# Auth endpoints
	path('vendor/authenticate', login_user, name='get_otp'),
	path('vendor/verify-otp', verify_otp, name='verify_otp'),
	path('vendor/password-reset', PasswordResetRequestView.as_view(), name='password-reset-request'),
    path('vendor/password-reset/confirm', PasswordResetView.as_view(), name='password-reset-confirm'),
	# Profile endpoints
	path('vendor/onboard/<str:action>', NewUserView.as_view(), name='onboarding'),
	path('vendor/profile', VendorProfileView.as_view(), name='vendor_profile'),
    path('vendor/password-change', PasswordChangeView.as_view(), name='password-change'),
	# Purchase Order endpoints
	path('vendor/purchaseorders', get_vendors_orders, name="get_purchase_orders"),
	path('vendor/purchaseorders/<int:po_id>', get_vendors_orders, name="get_purchase_order"),
	#GRN endpoints
	path('vendor/grns', get_vendors_grns, name="get_vendors_grns"),
	path('vendor/grns/download', download_vendor_grns, name="download_vendor_grns"),
	# Invoice endpoints
	path('vendor/invoices', VendorInvoiceView.as_view(), name='vendor_invoice'),
	# Misc endpoints
	path('surcharges', get_surcharges, name='get_surcharges'),
	
	# Approval endpoints
	path('approvals/createkey', KeystoreAPIView.as_view(), name='manage_keystore'),
]