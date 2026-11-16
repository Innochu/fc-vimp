"""Mock API for the flicker test. No login, no SAP: it uses auth.Group rows as
mock data and exercises the same two caches the real app uses:
  - django-cachalot, which caches ORM query results in the default cache
  - an explicit cache.get/cache.set with a 5 minute TTL, as the approval list does
"""
import os
import uuid

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt

PREFIX = 'flicker-test-'
VIEW_KEY = 'flicker_view_count'


def _raw_count():
	# Raw SQL, so it is not cached by cachalot: this isolates the explicit cache.
	with connection.cursor() as cursor:
		cursor.execute("SELECT COUNT(*) FROM auth_group WHERE name LIKE %s", [PREFIX + '%'])
		return cursor.fetchone()[0]


def count(request):
	orm_count = Group.objects.filter(name__startswith=PREFIX).count()  # cachalot-cached
	view_count = cache.get(VIEW_KEY)
	if view_count is None:
		view_count = _raw_count()
		cache.set(VIEW_KEY, view_count, 300)
	return JsonResponse({'pid': os.getpid(), 'orm': orm_count, 'view': view_count})


@csrf_exempt
def add(request):
	Group.objects.create(name=PREFIX + uuid.uuid4().hex)
	# Mirrors what the real app does after a write: clear its own cache entry.
	cache.delete(VIEW_KEY)
	return JsonResponse({'pid': os.getpid()})


@csrf_exempt
def cleanup(request):
	Group.objects.filter(name__startswith=PREFIX).delete()
	cache.delete(VIEW_KEY)
	return JsonResponse({'pid': os.getpid()})


urlpatterns = [
	path('count/', count),
	path('add/', add),
	path('cleanup/', cleanup),
]
