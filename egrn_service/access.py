from django.db.models import Q

from .models import Store


def get_user_stores(user):
	'''
		Stores (retail stores and SCD locations) a user may receive goods for.

		Retail stores are matched on `store_email`, as before. SCD locations are
		also granted through StoreAuthorization (manager/assistant/clerk) or a
		SourceLocationAuthorization approver row whose location is the store's
		ByD code, so SCD staff are not tied to one shared mailbox.
	'''
	# Imported here because transfer_service imports egrn_service models.
	from transfer_service.models import StoreAuthorization, SourceLocationAuthorization

	query = Q(store_email=user.email)
	query |= Q(
		location_type=Store.LOCATION_TYPE_SCD,
		id__in=StoreAuthorization.objects.filter(
			user=user, role__in=['manager', 'assistant', 'clerk']
		).values('store_id'),
	)
	query |= Q(
		location_type=Store.LOCATION_TYPE_SCD,
		byd_cost_center_code__in=SourceLocationAuthorization.objects.filter(
			user=user, role='approver'
		).values('source_location_id'),
	)
	return Store.objects.filter(query).distinct()
