import logging

from django.conf import settings

from .models import GoodsServiceReceipt

logger = logging.getLogger(__name__)


def post_gsr_to_byd(receipt_id: int):
	'''
		Send a submitted GSR to SAP ByD as the service confirmation.

		Disabled until the ByD object used for maintenance confirmations is agreed
		(Service Acknowledgement vs Service Entry Sheet). While GSR_BYD_POSTING_ENABLED
		is False the receipt simply stays "submitted".
	'''
	receipt = GoodsServiceReceipt.objects.get(id=receipt_id)
	if not getattr(settings, 'GSR_BYD_POSTING_ENABLED', False):
		logger.info(f"GSR {receipt.gsr_number}: ByD posting disabled, left as submitted.")
		return 'skipped'

	try:
		# TODO: build the payload and call the ByD service once the object is confirmed.
		raise NotImplementedError("ByD service confirmation is not configured yet.")
	except Exception as e:
		logger.error(f"GSR {receipt.gsr_number}: ByD posting failed: {e}")
		receipt.status = 'failed'
		receipt.status_reason = str(e)[:500]
		receipt.save(update_fields=['status', 'status_reason'])
		raise
