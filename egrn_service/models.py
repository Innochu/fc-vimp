import logging
import inspect
from . import converters
from .services import Middleware
from django.db import models
from django.db.utils import IntegrityError
from core_service.models import VendorProfile
from byd_service.rest import RESTServices
from byd_service.util import to_python_time
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Sum
from django.forms.models import model_to_dict
from django_q.tasks import async_task
from django.utils import timezone

# Initialize REST services
byd_rest_services = RESTServices()

def get_conversion_methods():
	methods = inspect.getmembers(converters, inspect.isfunction)
	return [(name, name) for name, func in methods]


# Create your models here.
class Surcharge(models.Model):
	code = models.IntegerField(verbose_name='Code')
	description = models.CharField(max_length=255, verbose_name='Description')
	type = models.CharField(max_length=50, blank=False, null=False, default="Value Added Tax", verbose_name='Type')
	rate = models.FloatField(verbose_name='Rate')
	last_modified = models.DateTimeField(auto_now=True, verbose_name='Last Modified')
	metadata = models.JSONField(default=dict, blank=True, null=True)
	
	def __str__(self):
		return f'{self.code} - {self.description}'
	
	class Meta:
		verbose_name = "Surcharge"
		verbose_name_plural = "Surcharges"


class ProductSurcharge(models.Model):
	'''
		Associates a product with a surcharge.
	'''
	
	product_id = models.CharField(max_length=32, blank=False, null=False) # The ByD Product ID
	surcharge = models.ForeignKey(Surcharge, on_delete=models.CASCADE, related_name='product_surcharge')
	
	class Meta:
		unique_together = ('product_id', 'surcharge')
		verbose_name = "Products - Surcharge"
		verbose_name_plural = "Products - Surcharges"


class Store(models.Model):
	'''
		Stores information about a store.
	'''
	LOCATION_TYPE_RETAIL = 'retail'
	LOCATION_TYPE_SCD = 'scd'
	LOCATION_TYPE_CHOICES = [
		(LOCATION_TYPE_RETAIL, 'Retail Store'),
		(LOCATION_TYPE_SCD, 'SCD Location'),
	]

	store_name = models.CharField(max_length=255)
	store_email = models.EmailField(blank=True, null=True)
	location_type = models.CharField(
		max_length=10, choices=LOCATION_TYPE_CHOICES, default=LOCATION_TYPE_RETAIL,
		help_text="SCD locations receive vendor-direct supplies from the SCD portal."
	)
	post_to_icg = models.BooleanField(
		default=True,
		help_text="Set to No only if this location isn't tracked in ICG. "
				  "Goods received here will then not be posted to ICG."
	)
	icg_warehouse_name = models.CharField(max_length=255, null=True, blank=True)
	icg_warehouse_code = models.CharField(max_length=20, unique=True)
	byd_cost_center_code = models.CharField(max_length=20, unique=True)
	# Record the full data incase any other key is added in the future
	metadata = models.JSONField(default=dict, blank=True, null=True)
	
	@property
	def default_store(self):
		'''
			Returns the default store record which is alwayws the first record in the database.
		'''
		first_store = Store.objects.first()
		return first_store
	
	def create_store(self, store_data):
		'''
			Creates a new store record from the given data.
		'''
		self.store_name = store_data.get('store_name', '')
		self.store_email = store_data.get('store_email', '')
		self.icg_warehouse_name = store_data.get('icg_warehouse_name', '')
		self.icg_warehouse_code = store_data.get('icg_warehouse_code', '')
		self.byd_cost_center_code = store_data.get('byd_cost_center_code', '')
		location_type = store_data.get('location_type')
		if location_type in dict(self.LOCATION_TYPE_CHOICES):
			self.location_type = location_type
		self.metadata = store_data
		# Save
		self.save()
		# Return the created store
		return self
	
	def __str__(self):
		return f"{self.store_name.upper()} | {self.icg_warehouse_name.upper()}"
	
	class Meta:
		verbose_name = 'Store'
		verbose_name_plural = 'Stores'


class PurchaseOrder(models.Model):
	vendor = models.ForeignKey(VendorProfile, on_delete=models.CASCADE)
	object_id = models.CharField(max_length=32, blank=False, null=False, unique=True)
	po_id = models.IntegerField(blank=False, null=False, unique=True)
	total_net_amount = models.DecimalField(max_digits=15, decimal_places=3, blank=False, null=False)
	date = models.DateField()
	metadata = models.JSONField(default=dict)
	
	delivery_status_code = [('1', 'Not Delivered'), ('2', 'Partially Delivered'), ('3', 'Completely Delivered')]
	
	@property
	def delivery_status(self, ):
		status = self.delivery_status_code[0]
		# Retrieve all related PurchaseOrderLineItems
		order_items = self.line_items.all()
		# Check if all PurchaseOrderLineItems are completed
		partially_delivered_quantities = [item.delivery_status[0] == '2' for item in order_items]
		completely_delivered_quantities = [item.delivery_status[0] == '3' for item in order_items]
		if all(completely_delivered_quantities):
			status = self.delivery_status_code[2]
		elif any(partially_delivered_quantities) or any(completely_delivered_quantities):
			status = self.delivery_status_code[1]
			
		return status
  
	
	def create_purchase_order(self, po):
		# Get the vendor's profile (if they've completed their onboarding), or create a profile that will be attached
		# to the vendor whenever they complete their onboarding.
		supplier = po.get("Supplier")
		vendor, created = VendorProfile.objects.get_or_create(byd_internal_id=supplier["PartyID"])
		if created:
			vendor.byd_metadata = supplier
			vendor.save()
		
		self.vendor = vendor
		self.object_id = po["ObjectID"]
		self.total_net_amount = po["TotalNetAmount"]
		self.po_id = po["ID"]
		self.date = to_python_time(po["LastChangeDateTime"])
		po_items = po.pop("Item")
		self.metadata = po
		self.save()
		
		created = 0
		try:
			for line_item in po_items:
				self.__create_line_items__(line_item)
				created += 1
		except Exception as e:
			self.delete()
			raise Exception(f"Error creating line items for purchase order: {e}")
		if created == 0:
			self.delete()
			raise Exception("No line items were created for purchase order.")
		return self
	
	def __create_line_items__(self, line_item):
		po_line_item = PurchaseOrderLineItem()
		
		po_line_item.purchase_order = self
		po_line_item.object_id = line_item["ObjectID"]
		po_line_item.product_name = line_item["Description"]
		po_line_item.product_id = line_item["ProductID"]
		po_line_item.quantity = float(line_item["Quantity"])
		po_line_item.unit_price = line_item["ListUnitPriceAmount"]
		po_line_item.unit_of_measurement = line_item["QuantityUnitCodeText"]
		po_line_item.metadata = line_item
		
		po_line_item.save()
	
	def __str__(self):
		return f"PO-{self.po_id}"
	
	class Meta:
		verbose_name_plural = "2.1 Purchase Orders"


class PurchaseOrderLineItem(models.Model):
	purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='line_items')
	delivery_store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='store_orders', blank=True, null=True)
	object_id = models.CharField(max_length=32, blank=False, null=False, unique=True)
	product_id = models.CharField(max_length=32, blank=False, null=False)
	product_name = models.CharField(max_length=100)
	quantity = models.DecimalField(max_digits=15, decimal_places=3)
	unit_price = models.DecimalField(max_digits=15, decimal_places=3)
	tax_rates = models.JSONField(default=list)
	unit_of_measurement = models.CharField(max_length=32, blank=False, null=False)
	metadata = models.JSONField(default=dict)
	
	@property
	def delivery_status(self):
		if self.delivered_quantity == 0:
			return self.purchase_order.delivery_status_code[0]
		elif (self.delivered_quantity > 0) and (self.delivered_quantity < self.quantity):
			return self.purchase_order.delivery_status_code[1]
		elif self.delivered_quantity == self.quantity:
			return self.purchase_order.delivery_status_code[2]
	
	@property
	def delivered_quantity(self, ):
		# Access related GoodsReceivedLineItem instances and calculate total received quantity
		delivered_quantity = self.grn_line_item.aggregate(total_received=Sum('quantity_received'))['total_received']
		return delivered_quantity or 0.0000
	
	@property
	def extra_fields(self, ):
		# If the product ID is defined in the ProductConversion model, return the conversion fields
		try:
			product_conversion = ProductConfiguration.objects.get(product_id=self.metadata["ProductID"])
			return product_conversion.conversion.conversion_field
		except ObjectDoesNotExist:
			return []
		except AttributeError:
			return []
	
	def __get_tax_rate__(self,):
		# Calculate the gross amount and tax rate based on the metadata['NetAmount'] and metadata['TaxAmount'] keys.
		net_amount = float(self.metadata['NetAmount'])
		tax_amount = float(self.metadata['TaxAmount'])
		gross_amount = net_amount + tax_amount
		tax_percentage =  (tax_amount / net_amount) * 100
		
		return round(tax_percentage, 1)
	
	def __get_delivery_store_details__(self, ):
		'''
			Retrieve the delivery store details from the metadata['DeliveryStoreDetails'] key.
		'''
		store = Store
		delivery_store_id = self.metadata['ItemShipToLocation']['LocationID']
		try:
			delivery_store = store.objects.get(byd_cost_center_code=delivery_store_id)
		except ObjectDoesNotExist:
			middleware = Middleware()
			store_data = middleware.get_store(byd_cost_center_code=delivery_store_id)
			# If the store is not found, create a new store or use the default store
			if store_data:
				delivery_store = store().create_store(store_data[0])
			else:
				raise Store.DoesNotExist("Store not found.")
		
		return delivery_store
	
	def save(self, ):
		try:
			# Get the surcharge with the tax rate
			surcharge = Surcharge.objects.filter(rate=self.__get_tax_rate__())
			self.tax_rates = [model_to_dict(i) for i in surcharge]
			self.delivery_store = self.__get_delivery_store_details__()
		except ObjectDoesNotExist as e:
			return False
			
		super().save()
	
	def __str__(self):
		return f"PO-{self.purchase_order.po_id}: {self.product_name} ({self.quantity}) for {self.delivery_store.store_name}"
	
	class Meta:
		verbose_name_plural = "2.2 Purchase Order Line Items"


class GoodsReceivedNote(models.Model):
	purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='purchase_order')
	grn_number = models.IntegerField(blank=False, null=False, unique=True)
	created = models.DateField(auto_now_add=True)
	inbound_delivery_object_id = models.CharField(max_length=255, blank=True, null=True)
	inbound_delivery_notification_id = models.CharField(max_length=255, blank=True, null=True)
	inbound_delivery_metadata = models.JSONField(default=dict, blank=True)
	is_nullified = models.BooleanField(default=False)
	nullified_on = models.DateTimeField(blank=True, null=True)
	nullified_reason = models.CharField(max_length=255, blank=True, null=True)
	
	invoicing_status_code = [('1', 'Not Started'), ('2', 'In Process'), ('3', 'Finished')]
	
	@property
	def stores(self):
		return set(store.delivery_store for store in self.line_items.all())
	
	@property
	def total_net_value_received(self,):
		return sum([item.net_value_received for item in self.line_items.all()])
	
	@property
	def total_gross_value_received(self,):
		return sum([item.gross_value_received for item in self.line_items.all()])
	
	@property
	def total_tax_value_received(self,):
		return self.total_gross_value_received - self.total_net_value_received
	
	@property
	def invoice_status(self):
		# If all related GoodsReceivedLineItem instances have is_invoiced as True, return 'Finished'
		if all(item.is_invoiced for item in self.line_items.all()):
			return self.invoicing_status_code[2]
		# If any related GoodsReceivedLineItem instances have is_invoiced as True, return 'In Process'
		if any(item.is_invoiced for item in self.line_items.all()):
			return self.invoicing_status_code[1]
		# If no related GoodsReceivedLineItem instances have is_invoiced as True, return 'Not Started'
		return self.invoicing_status_code[0]
		
	@property
	def invoice_status_code(self):
		return self.invoice_status[0]
	
	@property
	def invoice_status_text(self):
		return self.invoice_status[1]
	
	@property
	def invoiced_quantity(self):
		total_invoiced = 0.0000
		invoice_items = self.line_items.all()
		for item in invoice_items:
			total_invoiced += float(item.invoiced_quantity)
		return total_invoiced
	
	def save(self, *args, **kwargs):
		grn_data = kwargs.pop('grn_data', None)
		if not grn_data:
			return super().save(*args, **kwargs)
		po_id = grn_data['po_id']
		try:
			# Try to retrieve an object by a specific field if the object is found, you can work with it here
			self.purchase_order = PurchaseOrder.objects.get(po_id=po_id)
		except ObjectDoesNotExist:
			# Create the Purchase Order
			po_data = byd_rest_services.get_purchase_order_by_id(po_id)
			new_po = PurchaseOrder()
			self.purchase_order = new_po.create_purchase_order(po_data)
		except Exception as e:
			raise e
		
		# Create the GRN Number by appending a number to the end of the PO ID
		self.grn_number = int(str(po_id) + '1')
		# Progressively increment the GRN number until it is unique
		saved_as_grn = False # Boolean to control the loop
		while not saved_as_grn:
			try:
				super().save(*args, **kwargs)
				saved_as_grn = True
			except IntegrityError:
				self.grn_number = int(self.grn_number) + 1
			except Exception as e:
				logging.error(e)
				raise e
		# Delete the GRN if none of the line items were created (meaning there was an error with all the line items)
		try:
			self.__create_line_items__(grn_data.get("recievedGoods"))
		except Exception as e:
			self.delete()
			raise e
		# Perform asynchronous tasks after the GRN and it's corresponding line items have been created
		# Only post if at least one store involved is tracked in ICG.
		if any(store.post_to_icg for store in self.stores):
			async_task('vimp.tasks.post_to_icg', self, q_options={
				'task_name': f'Post-GRN-{self.grn_number}-To-ICG-Inventory',
			})
		# async_task('vimp.tasks.post_to_gl', {
		# 	'grn': self,
		# 	'action': 'receipt', # This must be one of either 'receipt' or 'invoice_approval'.
		# }, q_options={
		# 	'task_name': f'Post-GRN-{self.grn_number}-To-GL',
		# })
		async_task('vimp.tasks.send_grn_to_email', self, q_options={
			'task_name': f'Email-GRN-{self.grn_number}-To-Vendor',
		})
		async_task('vimp.tasks.create_inbound_delivery_notification_on_byd', self, q_options={
			'task_name': f'Create-Inbound-Notif-{self.grn_number}-On-ByD',
		})
		# Return the created Goods Received Note
		return self
	
	def mark_nullified(self, reason: str = ""):
		"""
			Flag the GRN as nullified and record metadata for audit purposes.
		"""
		self.is_nullified = True
		self.nullified_on = timezone.now()
		self.nullified_reason = reason or "Admin triggered nullification"
		self.save(update_fields=['is_nullified', 'nullified_on', 'nullified_reason'])

	def __create_line_items__(self, line_items):
		# An object to hold the status of line items that were created
		created_line_items = {}
		for line_item in line_items:
			try:
				grn_line_item = GoodsReceivedLineItem()
				# Get the purchase order line item that corresponds to this line item from the purchase order of this Goods Received Note
				grn_line_item.purchase_order_line_item = PurchaseOrderLineItem.objects.get(purchase_order=self.purchase_order,
																 object_id=line_item["itemObjectID"])
				grn_line_item.grn = self
				grn_line_item.quantity_received = round(float(line_item.get("quantityReceived") or 0),3)
				grn_line_item.save(data=line_item)
				created_line_items[line_item['itemObjectID']] = True
			except Exception as e:
				logging.error(f"{line_item['itemObjectID']}: {e}")
				created_line_items[line_item['itemObjectID']] = False
				raise e
		# If any of the line items were created, return True.
		return any(created_line_items.values())
	
	def __str__(self):
		return f"e-GRN #{self.grn_number}"
	
	class Meta:
		verbose_name_plural = "2.3 Goods Received Notes"


class GoodsReceivedLineItem(models.Model):
	grn = models.ForeignKey(GoodsReceivedNote, on_delete=models.CASCADE, related_name='line_items')
	purchase_order_line_item = models.ForeignKey(PurchaseOrderLineItem, on_delete=models.CASCADE,
												 related_name='grn_line_item')
	quantity_received = models.DecimalField(max_digits=15, decimal_places=3, default=0.000)
	net_value_received = models.DecimalField(max_digits=15, decimal_places=3)
	gross_value_received = models.DecimalField(max_digits=15, decimal_places=3)
	metadata = models.JSONField(default=dict, blank=True, null=True)
	date_received = models.DateField(auto_now=True)
	posted_to_icg = models.BooleanField(default=False)
	
	@property
	def delivery_store(self):
		return self.purchase_order_line_item.delivery_store
	
	@property
	def invoiced_quantity(self):
		invoiced_quantity = self.invoice_items.aggregate(total_quantity=Sum('quantity'))['total_quantity'] or 0.0000
		return invoiced_quantity
	
	@property
	def is_invoiced(self):
		return self.invoiced_quantity == self.quantity_received
	
	def net_value(self):
		return float(self.quantity_received) * float(self.purchase_order_line_item.unit_price)
	
	def calculate_tax_amount(self):
		'''
			Calculate the tax amount by getting the tax percentages from the purchase_order_line_item.tax_rates,
			and adding it to the net value received.
		'''
		tax_rates = sum([rate['rate'] for rate in self.purchase_order_line_item.tax_rates])
		tax_amount = self.net_value() * (tax_rates / 100)
		return round(tax_amount, 3)
	
	def calculate_weighted_average_cost(self):
		'''
			This should be calculated for every purchase using the formula:
			((Opening quantity * carrying value)+(purchase quantity * unit price))/(opening quantity + purchase quantity)
		'''
		...
	
	def clean(self):
		# Get the sum of the quantity received for this item by adding up the quantity received
		# of all GRN line items for this particular PO line item.
		grns_raised_for_this = self.get_grn_for_po_line(self.purchase_order_line_item.object_id)
		total_received = grns_raised_for_this.aggregate(total_sum=Sum('quantity_received'))['total_sum']
		total_received = total_received or 0.0000
		# Get the quantity that is being received for this item.
		quantity_to_receive = self.quantity_received
		# Check that quantity to receive is greater than 0.
		if quantity_to_receive <= 0:
			raise ValidationError("Quantity received must be greater than 0.")
		# Get the outstanding delivery for this item.
		outstanding_quantity = float(self.purchase_order_line_item.quantity) - float(total_received)
		# Check to see if there is any outstanding delivery for this item.
		if outstanding_quantity == 0:
			raise ValidationError("This item has been completely delivered.")
		# Get the sum of the quantity received and the total quantity received for this item.
		sum_quantity = float(quantity_to_receive) + float(total_received)
		# Check to see if the quantity received is greater than the outstanding quantity.
		if float(sum_quantity) > float(self.purchase_order_line_item.quantity):
			raise ValidationError(
				f"Quantity received ({quantity_to_receive}) is greater than outstanding delivery quantity ({outstanding_quantity}).")
		
	def convert_product(self, data):
		# Get the product_id of the product being saved from the po line item metadata`
		product_id = self.purchase_order_line_item.metadata.get('ProductID')
		try:
			# Get conversion methods defined for this product
			conversion_method = ProductConfiguration.objects.get(product_id=product_id).conversion.conversion_method
		except ObjectDoesNotExist:
			return False
		# Get the conversion method name from the instance
		method_name = conversion_method
		# Get all the functions from the conversion_methods module
		methods = dict(inspect.getmembers(converters, inspect.isfunction))
		# Get the specific method to call
		method_to_call = methods.get(method_name)
	
		if method_to_call:
			try:
				input_fields = data.get('extra_fields')
				# Call the conversion method with the Product instance
				result = method_to_call(input_fields=input_fields)
				# If any items in the result dict is an attribute of this class, remove it from the result dict and set it to the instance
				for key, value in result.items():
					if hasattr(self, key):
						setattr(self, key, value)
					else:
						self.metadata[key] = value
			except Exception as e:
				logging.error(f"Error converting product with method {method_name}: {e}")
				raise e
		else:
			logging.error(f"conversion method {method_name} not found in conversion_methods module")
	
	def get_grn_for_po_line(self, object_id):
		"""
			Returns the Goods Received Note for this line item.
		"""
		line_items = GoodsReceivedLineItem.objects.filter(purchase_order_line_item__object_id=object_id)
		return line_items
	
	def save(self, *args, **kwargs):
		"""
			Saves the instance to the database.
		"""
		try:
			self.convert_product(data=kwargs.get('data'))
		except Exception as e:
			logging.error(f"Error converting product: {e}")
			
		# Calculate the net and gross value received
		self.net_value_received = self.net_value()
		self.gross_value_received = self.net_value_received + self.calculate_tax_amount()
		
		self.clean()
		
		return super().save()
	
	def __str__(self):
		return f"e-GRN #{self.grn.grn_number}: '{self.purchase_order_line_item.product_name}'"
	
	class Meta:
		verbose_name_plural = "2.4 Goods Received Line Items"


class StockConsumptionRecord(models.Model):
	product_id = models.CharField(max_length=32)
	product_name = models.CharField(max_length=100, blank=True, null=True)
	quantity = models.DecimalField(max_digits=15, decimal_places=3)
	unit_cost = models.DecimalField(max_digits=15, decimal_places=3, default=0.0)
	unit_of_measurement = models.CharField(max_length=32, blank=True, null=True)
	cost_center = models.CharField(max_length=64, blank=True, null=True)
	external_item_id = models.CharField(max_length=64, blank=True, null=True)
	metadata = models.JSONField(default=dict, blank=True)
	date_consumed = models.DateTimeField(auto_now_add=True)

	@property
	def total_cost(self):
		return float(self.quantity) * float(self.unit_cost)

	def __str__(self):
		return f"{self.product_id} consumed ({self.quantity} {self.unit_of_measurement or ''})"

	class Meta:
		ordering = ['-date_consumed']
		verbose_name = '2.5 Stock Consumption Record'
		verbose_name_plural = '2.5 Stock Consumption Records'


class Conversion(models.Model):
	'''
		Defines how a product can be converted to another unit of measurement.
	'''
	name = models.CharField(max_length=100, blank=False, null=False, unique=True) # The name of the conversion.
	conversion_field = models.JSONField(default=dict, blank=False) # The fields that define the conversion.
	conversion_method = models.CharField(max_length=100, choices=get_conversion_methods()) #
	created_on = models.DateTimeField(auto_now_add=True)
	
	def __str__(self):
		return f"{self.name}"
	
	class Meta:
		verbose_name = "Products - Conversion"
		verbose_name_plural = "Products - Conversions"
	

class ProductConfiguration(models.Model):
	'''
		Associates a product with a conversion.
	'''
	product_id = models.CharField(max_length=32, blank=False, null=False, unique=True) # The ByD Product ID
	conversion = models.ForeignKey(Conversion, blank=True, null=True, on_delete=models.CASCADE, related_name='product_conversion')
	metadata = models.JSONField(default=dict, blank=True, null=True) # Additional metadata for the product.
	created_on = models.DateTimeField(auto_now_add=True)
	
	@property
	def product_name(self):
		product_order_instance = PurchaseOrderLineItem.objects.filter(product_id=self.product_id).first()
		return product_order_instance.product_name if product_order_instance else self.product_id
	
	def __str__(self):
		return f"'{self.product_id} ({self.product_name})'"
	
	class Meta:
		verbose_name = "Products - Configuration"
		verbose_name_plural = "Products - Configurations"