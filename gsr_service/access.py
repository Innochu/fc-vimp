from egrn_service.models import Store


def get_user_stores(user):
	'''
		Stores a user may confirm services for: those whose `store_email` is the
		user's email, the same rule the GRN endpoints use.
	'''
	return Store.objects.filter(store_email=user.email)
