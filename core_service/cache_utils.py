"""
Caching utilities for VIMP application.

This module provides smart caching strategies for expensive database operations,
API responses, and frequently accessed data.
"""

import hashlib
import json
from functools import wraps
from typing import Any, Optional, Union, Callable
from django.core.cache import cache
from django.conf import settings
from django.db.models import QuerySet
from django.http import HttpRequest
import logging

logger = logging.getLogger(__name__)

def _increment_cache_counter(counter_attr: str) -> None:
    """Increment a simple counter attribute on the django cache object for instrumentation."""
    try:
        setattr(cache, counter_attr, getattr(cache, counter_attr, 0) + 1)
    except Exception:
        # Never fail normal request flow due to instrumentation
        pass

class CacheManager:
    """
    Centralized cache management with intelligent key generation and invalidation.
    """
    
    # Cache timeout constants (in seconds)
    TIMEOUT_SHORT = 300      # 5 minutes
    TIMEOUT_MEDIUM = 1800    # 30 minutes
    TIMEOUT_LONG = 3600      # 1 hour
    TIMEOUT_DAILY = 86400    # 24 hours
    TIMEOUT_WEEKLY = 604800   # 1 week
    TIMEOUT_MONTHLY = 2592000 # 1 month
    TIMEOUT_YEARLY = 31536000 # 1 year
    TIMEOUT_FOREVER = None   # Cache till it is invalidated
    
    # Cache key prefixes
    PREFIX_QUERY = "query"
    PREFIX_API = "api"
    PREFIX_COUNT = "count"
    PREFIX_USER = "user"
    PREFIX_VENDOR = "vendor"
    PREFIX_WAC = "wac"  # Weighted Average Cost
    PREFIX_SIGNABLE = "signable"
    PREFIX_PO = "po"
    PREFIX_PO_LINE_ITEM = "po_line_item"
    PREFIX_GRN = "grn"
    PREFIX_GRN_LINE_ITEM = "grn_line_item"
    PREFIX_INVOICE = "invoice"
    PREFIX_INVOICE_LINE_ITEM = "invoice_line_item"
    
    
    @staticmethod
    def generate_cache_key(prefix: str, *args, **kwargs) -> str:
        """
        Generate a consistent cache key based on prefix and parameters.
        
        Args:
            prefix: Cache key prefix (e.g., 'query', 'api', 'count')
            *args: Positional arguments to include in key
            **kwargs: Keyword arguments to include in key
            
        Returns:
            str: Generated cache key
        """
        # Create a consistent string representation
        key_parts = [prefix]
        
        # Add positional arguments
        for arg in args:
            if hasattr(arg, 'pk'):  # Django model instance
                key_parts.append(f"{arg.__class__.__name__}_{arg.pk}")
            else:
                key_parts.append(str(arg))
        
        # Add keyword arguments (sorted for consistency)
        for key, value in sorted(kwargs.items()):
            key_parts.append(f"{key}_{value}")
        
        # Create hash if key is too long
        key_string = ":".join(key_parts)
        if len(key_string) > 200:  # Redis key length limit
            key_hash = hashlib.md5(key_string.encode()).hexdigest()
            return f"{prefix}:{key_hash}"
        
        return key_string
    
    @staticmethod
    def get_user_cache_key(user, prefix: str, *args) -> str:
        """Generate user-specific cache key."""
        user_id = getattr(user, 'id', 'anonymous')
        return CacheManager.generate_cache_key(
            f"{CacheManager.PREFIX_USER}_{user_id}_{prefix}", 
            *args
        )
    
    @staticmethod
    def get_vendor_cache_key(vendor, prefix: str, *args) -> str:
        """Generate vendor-specific cache key."""
        vendor_id = getattr(vendor, 'id', 'unknown')
        return CacheManager.generate_cache_key(
            f"{CacheManager.PREFIX_VENDOR}_{vendor_id}_{prefix}", 
            *args
        )
    
    @staticmethod
    def invalidate_pattern(pattern: str) -> int:
        """
        Invalidate all cache keys matching a pattern.

        Args:
            pattern: Pattern to match (e.g., 'user_123_*')

        Returns:
            int: Number of keys invalidated
        """
        try:
            # Import django_redis helper to get raw Redis client
            from django_redis import get_redis_connection

            # Get Redis client using the correct method for django-redis
            redis_client = get_redis_connection('default')

            # Build the full pattern including key prefix
            key_prefix = settings.CACHES['default'].get('KEY_PREFIX', '')
            if key_prefix:
                full_pattern = f"{key_prefix}:*{pattern}*"
            else:
                full_pattern = f"*{pattern}*"

            # Get all matching keys
            keys = redis_client.keys(full_pattern)
            if keys:
                # Delete all matching keys
                deleted_count = redis_client.delete(*keys)
                logger.info(f"Invalidated {deleted_count} cache keys matching pattern: {pattern}")
                return deleted_count
            return 0
        except ImportError:
            # Fallback if django_redis is not available
            logger.warning(f"django_redis not available, using cache.delete_pattern fallback")
            try:
                # Try using cache.delete_pattern if available
                if hasattr(cache, 'delete_pattern'):
                    return cache.delete_pattern(f"*{pattern}*")
            except Exception as fallback_error:
                logger.warning(f"Fallback cache invalidation also failed: {fallback_error}")
            return 0
        except Exception as e:
            logger.warning(f"Failed to invalidate cache pattern {pattern}: {e}")
            return 0


def cache_result(timeout: int = CacheManager.TIMEOUT_MEDIUM, 
                key_prefix: str = "result",
                user_specific: bool = False,
                vendor_specific: bool = False):
    """
    Decorator to cache function results.
    
    Args:
        timeout: Cache timeout in seconds
        key_prefix: Prefix for cache key
        user_specific: Include user ID in cache key
        vendor_specific: Include vendor ID in cache key
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Extract request if available
            request = None
            for arg in args:
                if isinstance(arg, HttpRequest):
                    request = arg
                    break
            
            # Generate cache key
            if user_specific and request and hasattr(request, 'user'):
                cache_key = CacheManager.get_user_cache_key(
                    request.user, key_prefix, func.__name__, *args[1:], **kwargs
                )
            elif vendor_specific and request and hasattr(request.user, 'vendor_profile'):
                cache_key = CacheManager.get_vendor_cache_key(
                    request.user.vendor_profile, key_prefix, func.__name__, *args[1:], **kwargs
                )
            else:
                cache_key = CacheManager.generate_cache_key(
                    key_prefix, func.__name__, *args, **kwargs
                )
            
            # Try to get from cache
            result = cache.get(cache_key)
            if result is not None:
                logger.debug(f"Cache hit for key: {cache_key}")
                _increment_cache_counter('_cache_hits')
                return result
            
            # Execute function and cache result
            logger.debug(f"Cache miss for key: {cache_key}")
            _increment_cache_counter('_cache_misses')
            result = func(*args, **kwargs)
            cache.set(cache_key, result, timeout)
            
            return result
        return wrapper
    return decorator


def cache_queryset_count(queryset: QuerySet, cache_key: str, 
                        timeout: int = CacheManager.TIMEOUT_SHORT) -> int:
    """
    Cache the count of a queryset to avoid expensive COUNT queries.
    
    Args:
        queryset: Django QuerySet to count
        cache_key: Cache key for storing the count
        timeout: Cache timeout in seconds
        
    Returns:
        int: Count of queryset
    """
    # Try to get count from cache
    count = cache.get(cache_key)
    if count is not None:
        logger.debug(f"Count cache hit for key: {cache_key}")
        _increment_cache_counter('_cache_hits')
        return count
    
    # Calculate count and cache it
    logger.debug(f"Count cache miss for key: {cache_key}")
    _increment_cache_counter('_cache_misses')
    count = queryset.count()
    cache.set(cache_key, count, timeout)
    
    return count


def get_or_set_cache(key: str, callable_obj: Callable, 
                    timeout: int = CacheManager.TIMEOUT_MEDIUM, 
                    *args, **kwargs) -> Any:
    """
    Get value from cache or set it by calling the provided function.
    
    Args:
        key: Cache key
        callable_obj: Function to call if cache miss
        timeout: Cache timeout in seconds
        *args: Arguments to pass to callable_obj
        **kwargs: Keyword arguments to pass to callable_obj
        
    Returns:
        Any: Cached or computed value
    """
    # Try to get from cache
    result = cache.get(key)
    if result is not None:
        logger.debug(f"Cache hit for key: {key}")
        _increment_cache_counter('_cache_hits')
        return result
    
    # Execute function and cache result
    logger.debug(f"Cache miss for key: {key}")
    _increment_cache_counter('_cache_misses')
    result = callable_obj(*args, **kwargs)
    cache.set(key, result, timeout)
    
    return result


def invalidate_user_cache(user_id: int, pattern: str = "") -> int:
    """
    Invalidate all cache entries for a specific user.
    
    Args:
        user_id: User ID to invalidate cache for
        pattern: Additional pattern to match
        
    Returns:
        int: Number of keys invalidated
    """
    pattern_to_invalidate = f"{CacheManager.PREFIX_USER}_{user_id}"
    if pattern:
        pattern_to_invalidate += f"_{pattern}"
    
    return CacheManager.invalidate_pattern(pattern_to_invalidate)


def invalidate_vendor_cache(vendor_id: int, pattern: str = "") -> int:
    """
    Invalidate all cache entries for a specific vendor.
    
    Args:
        vendor_id: Vendor ID to invalidate cache for
        pattern: Additional pattern to match
        
    Returns:
        int: Number of keys invalidated
    """
    pattern_to_invalidate = f"{CacheManager.PREFIX_VENDOR}_{vendor_id}"
    if pattern:
        pattern_to_invalidate += f"_{pattern}"
    
    return CacheManager.invalidate_pattern(pattern_to_invalidate)


class CachedPagination:
    """
    Helper class for caching pagination metadata and results.
    """
    
    @staticmethod
    def get_cached_page_count(queryset: QuerySet, page_size: int, 
                             cache_key_suffix: str) -> Optional[int]:
        """
        Get cached page count for a queryset.
        
        Args:
            queryset: Django QuerySet
            page_size: Items per page
            cache_key_suffix: Suffix for cache key
            
        Returns:
            Optional[int]: Number of pages or None if not cached
        """
        count_key = CacheManager.generate_cache_key(
            CacheManager.PREFIX_COUNT, cache_key_suffix
        )
        
        total_count = cache.get(count_key)
        if total_count is not None:
            _increment_cache_counter('_cache_hits')
            return (total_count + page_size - 1) // page_size  # Ceiling division
        
        return None
    
    @staticmethod
    def cache_page_count(queryset: QuerySet, cache_key_suffix: str,
                        timeout: int = CacheManager.TIMEOUT_SHORT) -> int:
        """
        Cache the total count for pagination.
        
        Args:
            queryset: Django QuerySet to count
            cache_key_suffix: Suffix for cache key
            timeout: Cache timeout in seconds
            
        Returns:
            int: Total count
        """
        count_key = CacheManager.generate_cache_key(
            CacheManager.PREFIX_COUNT, cache_key_suffix
        )
        
        return cache_queryset_count(queryset, count_key, timeout)