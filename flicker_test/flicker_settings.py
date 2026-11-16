"""Settings for the flicker test only. Never used by the real app.

Reuses the real settings, then swaps the URL conf for a tiny mock API and
chooses the cache backend from FLICKER_CACHE_MODE:
  old -> Django's default per-process LocMemCache (the original behaviour)
  new -> DummyCache (what vimp/settings.py now configures)
"""
import os

from vimp.settings import *  # noqa: F401,F403

ROOT_URLCONF = 'flicker_test.flicker_urls'
ALLOWED_HOSTS = ['*']

if os.getenv('FLICKER_CACHE_MODE', 'new') == 'old':
	CACHES = {
		'default': {
			'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
			'LOCATION': 'flicker-test',
		}
	}
else:
	CACHES = {
		'default': {
			'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
		}
	}
