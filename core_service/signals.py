"""
Cache invalidation signals for VIMP application.

This module handles automatic cache invalidation when models are created, updated, or deleted.
"""

from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from django.core.cache import cache
import logging

from .cache_utils import CacheManager, invalidate_user_cache, invalidate_vendor_cache

logger = logging.getLogger(__name__)

# Import models for signal handlers
from egrn_service.models import GoodsReceivedNote, GoodsReceivedLineItem, PurchaseOrder, Store
from invoice_service.models import Invoice
from approval_service.models import Signature, Keystore


@receiver([post_save, post_delete], sender=GoodsReceivedNote)
def invalidate_grn_cache(sender, instance, **kwargs):
    """
    Invalidate cache when GRN is created, updated, or deleted.
    
    This affects:
    - User GRN listings
    - Vendor GRN listings  
    - Weighted average calculations
    - Count caches
    """
    try:
        # Invalidate all GRN-related count caches
        CacheManager.invalidate_pattern("count:*grn*")
        
        # Invalidate weighted average caches (they depend on GRN data)
        CacheManager.invalidate_pattern(f"{CacheManager.PREFIX_WAC}:*")
        
        # Invalidate user-specific caches for all stores involved
        if hasattr(instance, 'line_items'):
            for line_item in instance.line_items.all():
                if hasattr(line_item, 'purchase_order_line_item') and line_item.purchase_order_line_item.delivery_store:
                    store = line_item.purchase_order_line_item.delivery_store
                    # Find users associated with this store and invalidate their caches
                    from core_service.models import CustomUser
                    users = CustomUser.objects.filter(email=store.store_email)
                    for user in users:
                        invalidate_user_cache(user.id, "grn")
        
        # Invalidate vendor-specific caches
        if hasattr(instance, 'purchase_order') and instance.purchase_order.vendor:
            vendor_id = instance.purchase_order.vendor.id
            invalidate_vendor_cache(vendor_id, "grn")
            
        logger.info(f"Invalidated cache for GRN {instance.id}")
        
    except Exception as e:
        logger.error(f"Error invalidating GRN cache: {e}")


@receiver([post_save, post_delete], sender=GoodsReceivedLineItem)
def invalidate_grn_line_item_cache(sender, instance, **kwargs):
    """
    Invalidate cache when GRN line item is created, updated, or deleted.
    
    This specifically affects weighted average calculations.
    """
    try:
        # Invalidate weighted average caches
        CacheManager.invalidate_pattern(f"{CacheManager.PREFIX_WAC}:*")
        
        # Invalidate product-specific caches if we can identify the product
        if hasattr(instance, 'purchase_order_line_item'):
            product_id = instance.purchase_order_line_item.product_id
            CacheManager.invalidate_pattern(f"*product_{product_id}*")
            
        logger.info(f"Invalidated cache for GRN line item {instance.id}")
        
    except Exception as e:
        logger.error(f"Error invalidating GRN line item cache: {e}")


@receiver([post_save, post_delete], sender=Invoice)
def invalidate_invoice_cache(sender, instance, **kwargs):
    """
    Invalidate cache when Invoice is created, updated, or deleted.

    This affects:
    - Invoice listings
    - Approval workflow for invoices
    - Count caches
    - Vendor-specific caches
    """
    try:
        # Invalidate all invoice-related caches
        CacheManager.invalidate_pattern("*invoice*")

        # Invalidate count caches
        CacheManager.invalidate_pattern("count:*")

        # Invalidate vendor-specific invoice caches
        if hasattr(instance, 'purchase_order') and instance.purchase_order.vendor:
            vendor_id = instance.purchase_order.vendor.id
            invalidate_vendor_cache(vendor_id, "invoice")

        logger.info(f"Invalidated cache for Invoice {instance.id}")

    except Exception as e:
        logger.error(f"Error invalidating Invoice cache: {e}")


@receiver([post_save, post_delete], sender=Store)
def invalidate_store_cache(sender, instance, **kwargs):
    """
    Invalidate cache when Store is created, updated, or deleted.
    """
    try:
        # Invalidate user store caches for the affected email
        from core_service.models import CustomUser
        users = CustomUser.objects.filter(email=instance.store_email)
        for user in users:
            invalidate_user_cache(user.id, "stores")
            
        # Also invalidate general store-related caches
        CacheManager.invalidate_pattern("*stores*")
        
        logger.info(f"Invalidated cache for Store {instance.id}")
        
    except Exception as e:
        logger.error(f"Error invalidating Store cache: {e}")


def _invalidate_user_stores_on_authorization_change(sender, instance, **kwargs):
    """
    A store/source-location authorization decides which stores a user can see,
    so drop their cached store list the moment it is granted, changed or removed.
    """
    try:
        invalidate_user_cache(instance.user_id, "stores")
    except Exception as e:
        logger.error(f"Error invalidating stores cache for user {instance.user_id}: {e}")


from transfer_service.models import StoreAuthorization, SourceLocationAuthorization
for _model in (StoreAuthorization, SourceLocationAuthorization):
    post_save.connect(_invalidate_user_stores_on_authorization_change, sender=_model,
                      dispatch_uid=f"invalidate_stores_{_model.__name__}")
    post_delete.connect(_invalidate_user_stores_on_authorization_change, sender=_model,
                        dispatch_uid=f"invalidate_stores_del_{_model.__name__}")


@receiver([post_save, post_delete], sender=PurchaseOrder)
def invalidate_purchase_order_cache(sender, instance, **kwargs):
    """
    Invalidate cache when Purchase Order is created, updated, or deleted.
    """
    try:
        # Invalidate vendor-specific caches
        if hasattr(instance, 'vendor') and instance.vendor:
            vendor_id = instance.vendor.id
            invalidate_vendor_cache(vendor_id)
            
        # Invalidate any PO-specific caches
        CacheManager.invalidate_pattern(f"*po_{instance.po_id}*")
        
        logger.info(f"Invalidated cache for Purchase Order {instance.id}")
        
    except Exception as e:
        logger.error(f"Error invalidating Purchase Order cache: {e}")


def clear_all_cache():
    """
    Utility function to clear all application caches.
    Use with caution - this will clear everything!
    """
    try:
        cache.clear()
        logger.info("Cleared all application caches")
        return True
    except Exception as e:
        logger.error(f"Error clearing all caches: {e}")
        return False


def warm_user_cache(user):
    """
    Pre-warm cache for a specific user with commonly accessed data.
    
    Args:
        user: User instance to warm cache for
    """
    try:
        from egrn_service.models import Store
        
        # Pre-load user stores
        user_stores_key = CacheManager.get_user_cache_key(
            user, "stores", user.email
        )
        from egrn_service.access import get_user_stores
        stores = list(get_user_stores(user))
        cache.set(user_stores_key, stores, CacheManager.TIMEOUT_LONG)
        
        logger.info(f"Warmed cache for user {user.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error warming cache for user {user.id}: {e}")
        return False


@receiver([post_save, post_delete], sender=Signature)
def invalidate_signature_cache(sender, instance, **kwargs):
    """
    Invalidate cache when Signature is created, updated, or deleted.

    This affects:
    - User signable listings
    - Signature tracking
    - Approval workflow caches
    - Pagination count caches
    """
    try:
        # Invalidate all approval-related caches
        CacheManager.invalidate_pattern("*signable*")
        CacheManager.invalidate_pattern("*signature*")
        CacheManager.invalidate_pattern("*track_signable*")

        # Invalidate count caches (critical for pagination)
        CacheManager.invalidate_pattern("count:*")

        # Invalidate user-specific caches
        if hasattr(instance, 'signer'):
            invalidate_user_cache(instance.signer.id, "signables")
            invalidate_user_cache(instance.signer.id, "permissions")

        logger.info(f"Invalidated cache for Signature {instance.id}")

    except Exception as e:
        logger.error(f"Error invalidating Signature cache: {e}")


@receiver([post_save, post_delete], sender=Keystore)
def invalidate_keystore_cache(sender, instance, **kwargs):
    """
    Invalidate cache when Keystore is created, updated, or deleted.
    """
    try:
        # Invalidate user's keystore cache
        if hasattr(instance, 'user'):
            invalidate_user_cache(instance.user.id, "keystore")
        
        logger.info(f"Invalidated cache for Keystore {instance.id}")
        
    except Exception as e:
        logger.error(f"Error invalidating Keystore cache: {e}")


def warm_vendor_cache(vendor):
    """
    Pre-warm cache for a specific vendor with commonly accessed data.
    
    Args:
        vendor: Vendor instance to warm cache for
    """
    try:
        # Could pre-load vendor-specific data here
        # For now, just log the operation
        logger.info(f"Warmed cache for vendor {vendor.id}")
        return True
        
    except Exception as e:
        logger.error(f"Error warming cache for vendor {vendor.id}: {e}")
        return False