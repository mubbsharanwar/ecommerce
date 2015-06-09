""" Tests of the Payment Views. """

import ddt
from django.core.urlresolvers import reverse
from django.test import TestCase
from django.test.client import RequestFactory
from factory.django import mute_signals
import httpretty
import mock
from oscar.apps.order.exceptions import UnableToPlaceOrder
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_model, get_class
from oscar.test import factories
from oscar.test.contextmanagers import mock_signal_receiver

from ecommerce.extensions.fulfillment.status import ORDER
from ecommerce.extensions.payment.processors.cybersource import Cybersource
from ecommerce.extensions.payment.processors.paypal import Paypal
from ecommerce.extensions.payment.tests.mixins import PaymentEventsMixin, CybersourceMixin, PaypalMixin
from ecommerce.extensions.payment.views import CybersourceNotifyView, PaypalPaymentExecutionView

Basket = get_model('basket', 'Basket')
Order = get_model('order', 'Order')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
SourceType = get_model('payment', 'SourceType')

post_checkout = get_class('checkout.signals', 'post_checkout')


@ddt.ddt
class CybersourceNotifyViewTests(CybersourceMixin, PaymentEventsMixin, TestCase):
    """ Test processing of CyberSource notifications. """

    def setUp(self):
        super(CybersourceNotifyViewTests, self).setUp()

        self.user = factories.UserFactory()
        self.billing_address = self.make_billing_address()

        self.basket = factories.create_basket()
        self.basket.owner = self.user
        self.basket.freeze()

        self.processor = Cybersource()
        self.processor_name = self.processor.NAME

    def assert_payment_data_recorded(self, notification):
        """ Ensure PaymentEvent, PaymentProcessorResponse, and Source objects are created for the basket. """

        # Ensure the response is stored in the database
        self.assert_processor_response_recorded(self.processor_name, notification[u'transaction_id'], notification,
                                                basket=self.basket)

        # Validate a payment Source was created
        reference = notification[u'transaction_id']
        source_type = SourceType.objects.get(code=self.processor_name)
        label = notification[u'req_card_number']
        self.assert_payment_source_exists(self.basket, source_type, reference, label)

        # Validate that PaymentEvents exist
        paid_type = PaymentEventType.objects.get(code='paid')
        self.assert_payment_event_exists(self.basket, paid_type, reference, self.processor_name)

    # Disable the normal signal receivers so that we can verify the state of the created order.
    @mute_signals(post_checkout)
    def test_accepted(self):
        """
        When payment is accepted, the following should occur:
            1. The response is recorded and PaymentEvent/Source objects created.
            2. An order for the corresponding basket is created.
            3. The order is fulfilled.
        """

        # The basket should not have an associated order if no payment was made.
        self.assertFalse(Order.objects.filter(basket=self.basket).exists())

        notification = self.generate_notification(
            self.processor.secret_key,
            self.basket,
            billing_address=self.billing_address,
        )

        with mock_signal_receiver(post_checkout) as receiver:
            response = self.client.post(reverse('cybersource_notify'), notification)

            # Validate that a new order exists in the correct state
            order = Order.objects.get(basket=self.basket)
            self.assertIsNotNone(order, 'No order was created for the basket after payment.')
            self.assertEqual(order.status, ORDER.OPEN)

            # Validate the order's line items
            self.assertListEqual(list(order.lines.values_list('product__id', flat=True)),
                                 list(self.basket.lines.values_list('product__id', flat=True)))

            # Verify the post_checkout signal was emitted
            self.assertEqual(receiver.call_count, 1)
            __, kwargs = receiver.call_args
            self.assertEqual(kwargs['order'], order)

        # The view should always return 200
        self.assertEqual(response.status_code, 200)

        # Validate the payment data was recorded for auditing
        self.assert_payment_data_recorded(notification)

        # The basket should be marked as submitted. Refresh with data from the database.
        basket = Basket.objects.get(id=self.basket.id)
        self.assertTrue(basket.is_submitted)
        self.assertIsNotNone(basket.date_submitted)

    @ddt.data('CANCEL', 'DECLINE', 'ERROR', 'blah!')
    def test_not_accepted(self, decision):
        """
        When payment is NOT accepted, the processor's response should be saved to the database. An order should NOT
        be created.
        """

        notification = self.generate_notification(self.processor.secret_key, self.basket, decision=decision)
        response = self.client.post(reverse('cybersource_notify'), notification)

        # The view should always return 200
        self.assertEqual(response.status_code, 200)

        # The basket should not have an associated order if no payment was made.
        self.assertFalse(Order.objects.filter(basket=self.basket).exists())

        # Ensure the response is stored in the database
        self.assert_processor_response_recorded(self.processor_name, notification[u'transaction_id'], notification,
                                                basket=self.basket)

    def test_unable_to_place_order(self):
        """ When payment is accepted, but an order cannot be placed, log an error and return HTTP 200. """

        notification = self.generate_notification(
            self.processor.secret_key,
            self.basket,
            billing_address=self.billing_address,
        )

        with mock.patch.object(CybersourceNotifyView, 'handle_order_placement', side_effect=UnableToPlaceOrder):
            response = self.client.post(reverse('cybersource_notify'), notification)

        # The view should always return 200
        self.assertEqual(response.status_code, 200)

        self.assert_processor_response_recorded(self.processor_name, notification[u'transaction_id'], notification,
                                                basket=self.basket)

    @ddt.data('abc', '1986', '')
    def test_invalid_basket(self, basket_id):
        """ When payment is accepted for a non-existent basket, log an error and record the response. """

        notification = self.generate_notification(
            self.processor.secret_key,
            self.basket,
            billing_address=self.billing_address,
            req_reference_number=basket_id,
        )
        response = self.client.post(reverse('cybersource_notify'), notification)

        self.assertEqual(response.status_code, 200)
        self.assert_processor_response_recorded(self.processor_name, notification[u'transaction_id'], notification)

    @ddt.data(('line2', 'foo'), ('state', 'bar'))
    @ddt.unpack
    @mock.patch('ecommerce.extensions.payment.views.CybersourceNotifyView.handle_order_placement')
    def test_optional_fields(self, field_name, field_value, mock_placement_handler):
        """ Ensure notifications are handled properly with or without keys/values present for optional fields. """

        def check_notification_address(notification, expected_address):
            response = self.client.post(reverse('cybersource_notify'), notification)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(mock_placement_handler.called)
            actual_address = mock_placement_handler.call_args[0][6]
            self.assertEqual(actual_address.summary, expected_address.summary)

        cybersource_key = 'req_bill_to_address_{}'.format(field_name)

        # Generate a notification without the optional field set.
        # Ensure that the Cybersource key does not exist in the notification,
        # and that the address our endpoint parses from the notification is
        # equivalent to the original.
        notification = self.generate_notification(
            self.processor.secret_key,
            self.basket,
            billing_address=self.billing_address,
        )
        self.assertNotIn(cybersource_key, notification)
        check_notification_address(notification, self.billing_address)

        # Add the optional field to the billing address in the notification.
        # Ensure that the Cybersource key now does exist, and that our endpoint
        # recognizes and parses it correctly.
        billing_address = self.make_billing_address({field_name: field_value})
        notification = self.generate_notification(
            self.processor.secret_key,
            self.basket,
            billing_address=billing_address,
        )
        self.assertIn(cybersource_key, notification)
        check_notification_address(notification, billing_address)

    def test_invalid_signature(self):
        """
        If the response signature is invalid, the view should return a 400. The response should be recorded, but an
        order should NOT be created.
        """
        notification = self.generate_notification(self.processor.secret_key, self.basket)
        notification[u'signature'] = u'Tampered'
        response = self.client.post(reverse('cybersource_notify'), notification)

        # The view should always return 200
        self.assertEqual(response.status_code, 400)

        # The basket should not have an associated order
        self.assertFalse(Order.objects.filter(basket=self.basket).exists())

        # The response should be saved.
        self.assert_processor_response_recorded(self.processor_name, notification[u'transaction_id'], notification,
                                                basket=self.basket)


class PaypalPaymentExecutionViewTests(PaypalMixin, PaymentEventsMixin, TestCase):
    """Test handling of users redirected by PayPal after approving payment."""

    def setUp(self):
        super(PaypalPaymentExecutionViewTests, self).setUp()

        self.basket = factories.create_basket()
        self.basket.owner = factories.UserFactory()
        self.basket.freeze()

        self.processor = Paypal()
        self.processor_name = self.processor.NAME

        # Dummy request from which an HTTP Host header can be extracted during
        # construction of absolute URLs
        self.request = RequestFactory().post('/')

    @httpretty.activate
    def _assert_execution_redirect(self):
        """Verify redirection to the configured receipt page after attempted payment execution."""
        self.mock_oauth2_response()

        # Create a payment record the view can use to retrieve a basket
        self.mock_payment_creation_response(self.basket)
        self.processor.get_transaction_parameters(self.basket, request=self.request)

        self.mock_payment_creation_response(self.basket, find=True)
        self.mock_payment_execution_response(self.basket)

        response = self.client.get(reverse('paypal_execute'), self.RETURN_DATA)
        self.assertRedirects(
            response,
            u'{}?basket_id={}'.format(self.processor.receipt_url, self.basket.id),
            fetch_redirect_response=False
        )

    def test_payment_execution(self):
        """Verify that a user who has approved payment is redirected to the configured receipt page."""
        self._assert_execution_redirect()

    def test_payment_error(self):
        """
        Verify that a user who has approved payment is redirected to the configured receipt page when payment
        execution fails.
        """
        with mock.patch.object(PaypalPaymentExecutionView, 'handle_payment',
                               side_effect=PaymentError) as fake_handle_payment:
            self._assert_execution_redirect()
            self.assertTrue(fake_handle_payment.called)

    def test_unable_to_place_order(self):
        """
        Verify that a user who has approved payment is redirected to the configured receipt page when the payment
        is executed but an order cannot be placed.
        """
        with mock.patch.object(PaypalPaymentExecutionView, 'handle_order_placement',
                               side_effect=UnableToPlaceOrder) as fake_handle_order_placement:
            self._assert_execution_redirect()
            self.assertTrue(fake_handle_order_placement.called)
