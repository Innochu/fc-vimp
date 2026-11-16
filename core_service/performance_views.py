"""
Performance monitoring views for VIMP application.

Provides endpoints for monitoring system performance, cache statistics,
and database metrics.
"""

from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db import connection
from django.conf import settings
import time
import psutil
import os

from .middleware import get_performance_metrics

@require_GET
# @staff_member_required
def performance_dashboard(request):
    """
    Performance monitoring dashboard endpoint.
    Only accessible to staff members.
    
    Returns comprehensive performance metrics including:
    - Database statistics
    - Cache statistics  
    - System resources
    - Application metrics
    """
    try:
        # Get basic performance metrics
        metrics = get_performance_metrics()
        
        # Add system metrics
        system_metrics = {
            'cpu_usage': psutil.cpu_percent(interval=1),
            'memory_usage': psutil.virtual_memory().percent,
            'disk_usage': psutil.disk_usage('/').percent,
            'process_memory': psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024,  # MB
        }
        
        # Database connection info
        db_metrics = {
            'total_queries': len(connection.queries),
            'connection_status': 'connected' if connection.connection else 'disconnected',
        }
        
        # Application-specific metrics
        app_metrics = {
            'debug_mode': settings.DEBUG,
            'cache_enabled': 'cachalot' in settings.INSTALLED_APPS,
            'redis_configured': 'django_redis' in str(settings.CACHES.get('default', {}).get('BACKEND', '')),
        }
        
        return JsonResponse({
            'status': 'success',
            'timestamp': time.time(),
            'system': system_metrics,
            'database': db_metrics,
            'application': app_metrics,
            'performance': metrics,
        })
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'error': str(e),
            'timestamp': time.time(),
        }, status=500)


@require_GET  
# @staff_member_required
def cache_statistics(request):
    """
    Detailed cache statistics endpoint.
    """
    try:
        from django_redis import get_redis_connection
        
        redis_conn = get_redis_connection("default")
        redis_info = redis_conn.info()
        
        # Extract relevant cache metrics
        cache_stats = {
            'memory': {
                'used_memory_human': redis_info.get('used_memory_human'),
                'used_memory_peak_human': redis_info.get('used_memory_peak_human'),
                'memory_fragmentation_ratio': redis_info.get('mem_fragmentation_ratio'),
            },
            'operations': {
                'total_commands_processed': redis_info.get('total_commands_processed'),
                'keyspace_hits': redis_info.get('keyspace_hits'),
                'keyspace_misses': redis_info.get('keyspace_misses'),
                'expired_keys': redis_info.get('expired_keys'),
                'evicted_keys': redis_info.get('evicted_keys'),
            },
            'connections': {
                'connected_clients': redis_info.get('connected_clients'),
                'total_connections_received': redis_info.get('total_connections_received'),
                'rejected_connections': redis_info.get('rejected_connections'),
            },
            'server': {
                'redis_version': redis_info.get('redis_version'),
                'uptime_in_seconds': redis_info.get('uptime_in_seconds'),
                'role': redis_info.get('role'),
            }
        }
        
        # Calculate hit ratio
        hits = redis_info.get('keyspace_hits', 0)
        misses = redis_info.get('keyspace_misses', 0)
        total = hits + misses
        hit_ratio = (hits / total * 100) if total > 0 else 0
        
        cache_stats['performance'] = {
            'hit_ratio_percentage': round(hit_ratio, 2),
            'total_requests': total,
        }
        
        return JsonResponse({
            'status': 'success',
            'cache_statistics': cache_stats,
            'timestamp': time.time(),
        })
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'error': str(e),
            'timestamp': time.time(),
        }, status=500)


@require_GET
# @staff_member_required  
def database_statistics(request):
    """
    Database performance statistics endpoint.
    """
    try:
        from django.db import connections
        
        db_stats = {}
        
        for alias, conn in connections.databases.items():
            try:
                # Test connection
                with connections[alias].cursor() as cursor:
                    # Basic connection info
                    db_stats[alias] = {
                        'engine': conn['ENGINE'],
                        'name': conn['NAME'],
                        'host': conn.get('HOST', 'localhost'),
                        'port': conn.get('PORT', 'default'),
                        'connection_status': 'connected',
                    }
                    
                    # MySQL/MariaDB specific stats
                    if 'mysql' in conn['ENGINE'].lower():
                        cursor.execute("SHOW STATUS LIKE 'Threads_connected'")
                        connections_result = cursor.fetchone()
                        if connections_result:
                            db_stats[alias]['active_connections'] = connections_result[1]
                        
                        cursor.execute("SHOW STATUS LIKE 'Queries'")
                        queries_result = cursor.fetchone()
                        if queries_result:
                            db_stats[alias]['total_queries'] = queries_result[1]
                            
                        cursor.execute("SHOW STATUS LIKE 'Uptime'")
                        uptime_result = cursor.fetchone()
                        if uptime_result:
                            db_stats[alias]['uptime_seconds'] = uptime_result[1]
                    
                    # PostgreSQL specific stats
                    elif 'postgresql' in conn['ENGINE'].lower():
                        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active'")
                        active_connections = cursor.fetchone()[0]
                        db_stats[alias]['active_connections'] = active_connections
                        
            except Exception as e:
                db_stats[alias] = {
                    'connection_status': 'error',
                    'error': str(e)
                }
        
        return JsonResponse({
            'status': 'success',
            'database_statistics': db_stats,
            'django_query_count': len(connection.queries),
            'timestamp': time.time(),
        })
        
    except Exception as e:
        return JsonResponse({
            'status': 'error', 
            'error': str(e),
            'timestamp': time.time(),
        }, status=500)


@require_GET
def health_check(request):
    """
    Simple health check endpoint for load balancers and monitoring.
    Does not require authentication.
    """
    try:
        # Basic checks
        checks = {
            'database': False,
            'cache': False,
            'disk_space': False,
        }
        
        # Test database connection
        try:
            from django.db import connection
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                checks['database'] = True
        except:
            pass
        
        # Test cache connection
        if settings.CACHES.get('default', {}).get('BACKEND', '').endswith('DummyCache'):
            # Caching is intentionally disabled (it stores nothing), so there is
            # nothing to check. Without this the endpoint would return 503 forever.
            checks['cache'] = True
        else:
            try:
                cache.set('health_check', 'ok', 10)
                if cache.get('health_check') == 'ok':
                    checks['cache'] = True
                    cache.delete('health_check')
            except:
                pass
        
        # Check disk space (warn if less than 10% free)
        try:
            disk_usage = psutil.disk_usage('/')
            free_percentage = (disk_usage.free / disk_usage.total) * 100
            checks['disk_space'] = free_percentage > 10
        except:
            pass
        
        # Overall health
        all_healthy = all(checks.values())
        
        return JsonResponse({
            'status': 'healthy' if all_healthy else 'degraded',
            'checks': checks,
            'timestamp': time.time(),
        }, status=200 if all_healthy else 503)
        
    except Exception as e:
        return JsonResponse({
            'status': 'error',
            'error': str(e),
            'timestamp': time.time(),
        }, status=500)